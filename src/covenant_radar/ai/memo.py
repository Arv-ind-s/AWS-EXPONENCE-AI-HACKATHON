"""Stage-7 grounded memo orchestration.

This module is the only model-facing memo path.  It assembles a prompt from a
provenance-carrying slot map, sends it through the guarded model client, and
returns a draft only after the product-owned stage-7 checks pass.  No value is
calculated here and no memo persistence is attempted; the persistence/refusal
transaction belongs to T-101.

The model receives masked text and may return masking tokens for copied driver
names or identifiers.  Those tokens are reconstructed on the host before
validation and display.  The original provider response remains available on
the result for audit callers, while the returned draft is explicitly labelled
as model-written.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from datetime import date, datetime
from decimal import Decimal
from math import isfinite
from types import MappingProxyType
from typing import Final
from uuid import UUID

from covenant_radar.ai.client import CallContext, ModelClient, ModelResult, Stage
from covenant_radar.ai.masking import MaskedPrompt, build_outbound
from covenant_radar.ai.prompts.loader import (
    DEFAULT_PROMPT_DIRECTORY,
    PromptLoader,
)
from covenant_radar.ai.shapes import (
    MAX_MEMO_OUTPUT_TOKENS,
    CatalogueAction,
    ShapeCheck,
    Stage7ShapeReport,
    check_stage7_shapes,
    normalise_catalogue_actions,
)
from covenant_radar.domain.memo.slots import MemoSlot, MemoSlotMap

__all__ = [
    "COMPONENT",
    "LEGACY_PROMPT_VERSION",
    "MEMO_DRAFT_LABEL",
    "MemoDraft",
    "MemoDraftingResult",
    "MemoReplyShapeError",
    "MemoShapeRefusal",
    "PROMPT_NAME",
    "PROMPT_VERSION",
    "T6_MAX_OUTPUT_TOKENS",
    "build_memo_prompt",
    "build_stage7_prompt",
    "draft_memo",
    "draft_stage7_memo",
    "parse_stage7_reply",
]

PROMPT_NAME: Final[str] = "stage7_memo"
PROMPT_VERSION: Final[str] = "v2"
LEGACY_PROMPT_VERSION: Final[str] = "v1"
COMPONENT: Final[str] = "stage7_memo"
MEMO_DRAFT_LABEL: Final[str] = "Drafted by model"
T6_MAX_OUTPUT_TOKENS: Final[int] = MAX_MEMO_OUTPUT_TOKENS

_MAX_REPLY_BYTES: Final[int] = 65_536
_ABSENT_TEXT: Final[str] = "Not available from the recorded evidence."
_MAX_RETRY_DETAIL_LENGTH: Final[int] = 2_000
_BASE_REPLY_KEYS: Final[frozenset[str]] = frozenset(
    {
        "headline",
        "summary",
        "drivers",
        "recommended_next_step",
        "disclaimer",
    }
)


@dataclass(frozen=True, slots=True)
class MemoDraft:
    """Strictly parsed stage-7 output, marked as model-authored prose."""

    headline: str
    summary: str
    drivers: tuple[str, ...]
    actions: tuple[Mapping[str, str], ...]
    recommended_next_step: str
    disclaimer: str
    raw_reply: str = ""

    def __post_init__(self) -> None:
        for name in ("headline", "summary", "recommended_next_step", "disclaimer"):
            value = getattr(self, name)
            if not isinstance(value, str) or not value.strip():
                raise ValueError(f"MemoDraft.{name} must be non-blank text.")
        drivers = tuple(self.drivers)
        if any(not isinstance(driver, str) or not driver.strip() for driver in drivers):
            raise ValueError("MemoDraft.drivers must contain non-blank text names.")
        actions: list[Mapping[str, str]] = []
        for action in self.actions:
            if not isinstance(action, Mapping):
                raise TypeError("MemoDraft.actions must contain mapping values.")
            identifier = action.get("id")
            role_tag = action.get("role_tag")
            if not isinstance(identifier, str) or not identifier.strip():
                raise ValueError("MemoDraft action id must be non-blank text.")
            if not isinstance(role_tag, str) or not role_tag.strip():
                raise ValueError("MemoDraft action role_tag must be non-blank text.")
            actions.append(
                MappingProxyType({"id": identifier.strip(), "role_tag": role_tag.strip()})
            )
        if not isinstance(self.raw_reply, str):
            raise TypeError("MemoDraft.raw_reply must be text.")
        object.__setattr__(self, "drivers", drivers)
        object.__setattr__(self, "actions", tuple(actions))

    @property
    def drafted_by_model(self) -> bool:
        """Whether the interface must show the model-authored label."""

        return True

    @property
    def label(self) -> str:
        return MEMO_DRAFT_LABEL

    @property
    def text(self) -> str:
        """Return the prose portion in fixed display order."""

        return "\n\n".join(
            (self.headline, self.summary, self.recommended_next_step, self.disclaimer)
        )

    def as_mapping(self) -> dict[str, object]:
        """Return the validated reply in persistence-friendly JSON shape."""

        return {
            "headline": self.headline,
            "summary": self.summary,
            "drivers": list(self.drivers),
            "actions": [dict(action) for action in self.actions],
            "recommended_next_step": self.recommended_next_step,
            "disclaimer": self.disclaimer,
        }


@dataclass(frozen=True, slots=True)
class MemoDraftingResult:
    """A checked draft and the model-call provenance needed by T-101."""

    draft: MemoDraft
    shape_report: Stage7ShapeReport
    model_call_id: UUID
    attempts: int
    provider_result: ModelResult

    def __post_init__(self) -> None:
        if not isinstance(self.draft, MemoDraft):
            raise TypeError("MemoDraftingResult.draft must be a MemoDraft.")
        if not isinstance(self.shape_report, Stage7ShapeReport):
            raise TypeError("MemoDraftingResult.shape_report must be a Stage7ShapeReport.")
        if not isinstance(self.model_call_id, UUID):
            raise TypeError("MemoDraftingResult.model_call_id must be a UUID.")
        if (
            isinstance(self.attempts, bool)
            or not isinstance(self.attempts, int)
            or self.attempts < 1
        ):
            raise ValueError("MemoDraftingResult.attempts must be a positive integer.")
        if not isinstance(self.provider_result, ModelResult):
            raise TypeError("MemoDraftingResult.provider_result must be a ModelResult.")
        if not self.shape_report.all_passed:
            raise ValueError("A MemoDraftingResult cannot contain a failed shape report.")

    @property
    def shape_checks(self) -> tuple[ShapeCheck, ...]:
        return self.shape_report.checks

    @property
    def text(self) -> str:
        return self.draft.text

    @property
    def label(self) -> str:
        return self.draft.label

    @property
    def drafted_by_model(self) -> bool:
        return self.draft.drafted_by_model

    def as_mapping(self) -> dict[str, object]:
        return {
            "draft": self.draft.as_mapping(),
            "shape_report": {
                "verdict": self.shape_report.verdict,
                "failed_checks": list(self.shape_report.failed_checks),
            },
            "model_call_id": str(self.model_call_id),
            "attempts": self.attempts,
            "label": self.label,
        }


class MemoReplyShapeError(ValueError):
    """The provider response is not the declared stage-7 JSON shape."""


class MemoShapeRefusal(ValueError):
    """Both permitted stage-7 attempts failed product-owned checks."""

    def __init__(
        self,
        message: str,
        *,
        report: Stage7ShapeReport,
        attempts: int,
        last_reply: str,
    ) -> None:
        super().__init__(message)
        self.report = report
        self.attempts = attempts
        self.last_reply = last_reply


def build_memo_prompt(
    slots: MemoSlotMap,
    catalogue: Iterable[object] | Mapping[str, object] | object,
    *,
    prompt_loader: PromptLoader | None = None,
    prompt_version: str = PROMPT_VERSION,
) -> MaskedPrompt:
    """Build the provider prompt from slots and permitted catalogue facts."""

    if not isinstance(slots, MemoSlotMap):
        raise TypeError("build_memo_prompt requires a MemoSlotMap.")
    if not isinstance(prompt_version, str) or not prompt_version.strip():
        raise ValueError("prompt_version must be non-blank text.")
    actions = normalise_catalogue_actions(catalogue)
    loader = prompt_loader or PromptLoader(DEFAULT_PROMPT_DIRECTORY)
    template = loader.load(PROMPT_NAME, prompt_version)

    outbound_fields = _outbound_fields(slots, actions)
    masked = build_outbound(outbound_fields, prompt_version=prompt_version)
    render_values = _render_values(slots, actions, masked)
    rendered = template.render({name: render_values[name] for name in template.placeholders})
    return MaskedPrompt(
        content=rendered,
        version=prompt_version,
        fields=masked.fields,
        token_map=masked.token_map,
    )


build_stage7_prompt = build_memo_prompt


def parse_stage7_reply(
    raw_reply: str | Mapping[str, object], *, require_actions: bool = False
) -> MemoDraft:
    """Parse one strict stage-7 reply without making a trust decision.

    ``require_actions=False`` keeps the v1 five-field shape readable for
    historical cassettes.  The current v2 drafting path enables the stricter
    action-citation field declared by its prompt.
    """

    if not isinstance(require_actions, bool):
        raise TypeError("require_actions must be boolean.")

    if isinstance(raw_reply, Mapping):
        payload = dict(raw_reply)
        raw_text = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        if len(raw_text.encode("utf-8")) > _MAX_REPLY_BYTES:
            raise MemoReplyShapeError(f"Stage-7 reply exceeds {_MAX_REPLY_BYTES}-byte limit.")
    elif isinstance(raw_reply, str):
        raw_text = raw_reply
        if len(raw_reply.encode("utf-8")) > _MAX_REPLY_BYTES:
            raise MemoReplyShapeError(f"Stage-7 reply exceeds {_MAX_REPLY_BYTES}-byte limit.")
        try:
            parsed = json.loads(raw_reply)
        except json.JSONDecodeError as error:
            raise MemoReplyShapeError(f"Stage-7 reply is not valid JSON: {error}.") from error
        if not isinstance(parsed, dict):
            raise MemoReplyShapeError("Stage-7 reply must be one JSON object.")
        payload = parsed
    else:
        raise TypeError("parse_stage7_reply requires JSON text or a mapping.")

    keys = set(payload)
    allowed = _BASE_REPLY_KEYS | {"actions", "recommended_actions"}
    missing = sorted(_BASE_REPLY_KEYS - keys)
    unexpected = sorted(keys - allowed)
    if missing or unexpected:
        details: list[str] = []
        if missing:
            details.append("missing " + ", ".join(missing))
        if unexpected:
            details.append("unexpected " + ", ".join(unexpected))
        raise MemoReplyShapeError(
            "Stage-7 reply does not match its declared shape: " + "; ".join(details)
        )
    if "actions" in payload and "recommended_actions" in payload:
        raise MemoReplyShapeError("Stage-7 reply may contain actions only once.")
    if require_actions and "actions" not in payload:
        raise MemoReplyShapeError("Stage-7 reply is missing required field 'actions'.")

    headline = _reply_text(payload, "headline")
    summary = _reply_text(payload, "summary")
    recommended_next_step = _reply_text(payload, "recommended_next_step")
    disclaimer = _reply_text(payload, "disclaimer")
    raw_drivers = payload["drivers"]
    if isinstance(raw_drivers, str | bytes | bytearray) or not isinstance(raw_drivers, Sequence):
        raise MemoReplyShapeError("Stage-7 reply field 'drivers' must be an array of text.")
    drivers = tuple(_array_text(raw_drivers, "drivers"))
    raw_actions = payload.get("actions", payload.get("recommended_actions", ()))
    actions = _parse_actions(raw_actions)
    return MemoDraft(
        headline=headline,
        summary=summary,
        drivers=drivers,
        actions=actions,
        recommended_next_step=recommended_next_step,
        disclaimer=disclaimer,
        raw_reply=raw_text,
    )


def draft_memo(
    slots: MemoSlotMap,
    catalogue: Iterable[object] | Mapping[str, object] | object,
    client: ModelClient,
    *,
    prompt_loader: PromptLoader | None = None,
    request_id: str | None = None,
    context: CallContext | None = None,
    max_attempts: int = 2,
    prompt_version: str = PROMPT_VERSION,
) -> MemoDraftingResult:
    """Generate, check, and return one grounded model-authored memo draft.

    A bad shape receives exactly one regeneration.  On the second failure the
    function raises :class:`MemoShapeRefusal`; it never returns a partial
    draft.  T-101 owns the surrounding database transaction and therefore can
    guarantee that this refusal creates no memo row.
    """

    if not isinstance(client, ModelClient):
        raise TypeError("draft_memo requires a ModelClient.")
    if (
        isinstance(max_attempts, bool)
        or not isinstance(max_attempts, int)
        or not 1 <= max_attempts <= 2
    ):
        raise ValueError("max_attempts must be one or two for the T8 retry policy.")
    if context is not None and not isinstance(context, CallContext):
        raise TypeError("context must be a CallContext.")

    actions = normalise_catalogue_actions(catalogue)
    prompt = build_memo_prompt(
        slots,
        actions,
        prompt_loader=prompt_loader,
        prompt_version=prompt_version,
    )
    call_context = _memo_context(context, request_id)
    last_reply = ""
    last_report: Stage7ShapeReport | None = None

    for attempt in range(1, max_attempts + 1):
        result = client.call(Stage.SEVEN, prompt, prompt_version, call_context)
        masked_reply = result.text or ""
        last_reply = _unmask(masked_reply, prompt.token_map)
        try:
            draft = parse_stage7_reply(
                last_reply,
                require_actions=prompt_version == PROMPT_VERSION,
            )
        except (MemoReplyShapeError, TypeError, ValueError) as error:
            report = check_stage7_shapes(last_reply, slots, actions)
            if report.all_passed:
                # A parser failure cannot become a pass through a weaker
                # representation; retain a named failure for retry/refusal.
                report = _parser_failure_report(report, str(error))
            last_report = report
        else:
            report = check_stage7_shapes(
                draft.as_mapping(),
                slots,
                actions,
                require_actions=prompt_version == PROMPT_VERSION,
            )
            last_report = report
            if report.all_passed:
                return MemoDraftingResult(
                    draft=draft,
                    shape_report=report,
                    model_call_id=result.model_call_id,
                    attempts=attempt,
                    provider_result=result,
                )

        if attempt < max_attempts:
            prompt = _retry_prompt(prompt, last_report)

    assert last_report is not None
    detail = "; ".join(last_report.failures) or "stage-7 reply failed the declared shape"
    raise MemoShapeRefusal(
        f"Stage-7 memo refused after {max_attempts} attempt(s): {detail}",
        report=last_report,
        attempts=max_attempts,
        last_reply=last_reply,
    )


draft_stage7_memo = draft_memo


def _outbound_fields(slots: MemoSlotMap, actions: Sequence[CatalogueAction]) -> dict[str, object]:
    fields: dict[str, object] = {
        "situation": _text_value(slots["situation"]),
        "evidence_counts_text": _slot_json_or_text(slots["evidence_counts"]),
        "simulation_options_text": _slot_json_or_text(slots["simulation_options"]),
        "recommended_interventions_text": _slot_json_or_text(slots["recommended_interventions"]),
        "intervention_text": _intervention_text(slots["intervention_text"]),
        "action_ids": [action.id for action in actions],
        "action_roles": [f"{action.id}:{action.role_tag}" for action in actions],
        "drivers": _driver_names(slots["drivers"]),
    }
    for slot_name in (
        "ratio_name",
        "value",
        "threshold",
        "headroom",
        "probability",
        "confidence",
        "crossing_date",
    ):
        slot = slots[slot_name]
        # An absent or suppressed slot still carries human-readable
        # placeholder text (`MemoSlot` requires explicit text instead of
        # null), which is meant for display, not for the model-bound
        # payload. Without this guard, e.g. a borrower whose covenant never
        # projects a crossing date sends its placeholder text
        # ("Not available from the recorded evidence.") down the
        # `crossing_date` slot, whose masking spec accepts any string and
        # then fails trying to parse it as an ISO date — turning every
        # borrower without a projected crossing into a 500 on memo
        # generation instead of a memo that simply omits the field.
        if not slot.resolved:
            continue
        value = slot.value
        if _outbound_value_is_valid(slot_name, value):
            fields[slot_name] = value
    return fields


def _render_values(
    slots: MemoSlotMap,
    actions: Sequence[CatalogueAction],
    masked: MaskedPrompt,
) -> dict[str, str]:
    def field(name: str, fallback: str) -> str:
        value = masked.fields.get(name, fallback)
        if isinstance(value, str | int | float | bool):
            return str(value)
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    return {
        "situation": field("situation", _text_value(slots["situation"])),
        "ratio_name": field("ratio_name", _text_value(slots["ratio_name"])),
        "value": field("value", _text_value(slots["value"])),
        "threshold": field("threshold", _text_value(slots["threshold"])),
        "headroom": field("headroom", _text_value(slots["headroom"])),
        "probability": field("probability", _text_value(slots["probability"])),
        "confidence": field("confidence", _text_value(slots["confidence"])),
        "crossing_date": field("crossing_date", _text_value(slots["crossing_date"])),
        "drivers": field("drivers", _slot_json_or_text(slots["drivers"])),
        "evidence_counts": field(
            "evidence_counts_text", _slot_json_or_text(slots["evidence_counts"])
        ),
        "simulation_options": field(
            "simulation_options_text", _slot_json_or_text(slots["simulation_options"])
        ),
        "recommended_interventions": field(
            "recommended_interventions_text",
            _slot_json_or_text(slots["recommended_interventions"]),
        ),
        "intervention_text": field(
            "intervention_text", _intervention_text(slots["intervention_text"])
        ),
        "action_ids": json.dumps([action.id for action in actions], ensure_ascii=False),
        "action_roles": json.dumps(
            [f"{action.id}:{action.role_tag}" for action in actions], ensure_ascii=False
        ),
    }


def _outbound_value_is_valid(name: str, value: object) -> bool:
    if value is None:
        return False
    if name in {"value", "threshold", "headroom", "probability", "confidence"}:
        if isinstance(value, bool) or not isinstance(value, Decimal | int | float):
            return False
        return (
            value.is_finite()
            if isinstance(value, Decimal)
            else isfinite(value)
            if isinstance(value, float)
            else True
        )
    if name == "crossing_date":
        return isinstance(value, date | str) and not isinstance(value, datetime)
    return isinstance(value, str)


def _text_value(slot: MemoSlot) -> str:
    value = slot.value
    if isinstance(value, str):
        return value
    return json.dumps(slot.as_mapping()["value"], ensure_ascii=False, separators=(",", ":"))


def _slot_json_or_text(slot: MemoSlot) -> str:
    if isinstance(slot.value, str):
        return slot.value
    return json.dumps(slot.as_mapping()["value"], ensure_ascii=False, separators=(",", ":"))


def _intervention_text(slot: MemoSlot) -> str:
    value = slot.value
    if isinstance(value, str):
        return value
    if isinstance(value, Sequence) and not isinstance(value, str | bytes | bytearray):
        texts = tuple(item for item in value if isinstance(item, str))
        if texts:
            return "\n".join(texts)
    return _ABSENT_TEXT


def _driver_names(slot: MemoSlot) -> list[str]:
    value = slot.value
    if not isinstance(value, Sequence) or isinstance(value, str | bytes | bytearray):
        return []
    result: list[str] = []
    for item in value:
        if isinstance(item, Mapping) and isinstance(item.get("name"), str):
            result.append(item["name"])
        elif isinstance(item, str):
            result.append(item)
    return result


def _reply_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise MemoReplyShapeError(f"Stage-7 reply field {key!r} must be non-blank text.")
    return value


def _array_text(value: Sequence[object], key: str) -> tuple[str, ...]:
    result: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise MemoReplyShapeError(f"Stage-7 reply field {key}[{index}] must be non-blank text.")
        result.append(item)
    return tuple(result)


def _parse_actions(value: object) -> tuple[Mapping[str, str], ...]:
    if (
        isinstance(value, Mapping)
        or isinstance(value, str | bytes | bytearray)
        or not isinstance(value, Sequence)
    ):
        raise MemoReplyShapeError("Stage-7 reply field 'actions' must be an array of objects.")
    result: list[Mapping[str, str]] = []
    for index, item in enumerate(value):
        if not isinstance(item, Mapping):
            raise MemoReplyShapeError(f"Stage-7 reply field actions[{index}] must be an object.")
        extra = set(item).difference({"id", "code", "role_tag"})
        if extra:
            names = ", ".join(sorted(str(name) for name in extra))
            raise MemoReplyShapeError(
                f"Stage-7 reply field actions[{index}] has unexpected field(s): {names}."
            )
        identifier = item.get("id", item.get("code"))
        role_tag = item.get("role_tag")
        if not isinstance(identifier, str) or not identifier.strip():
            raise MemoReplyShapeError(f"Stage-7 reply field actions[{index}].id is invalid.")
        if not isinstance(role_tag, str) or not role_tag.strip():
            raise MemoReplyShapeError(f"Stage-7 reply field actions[{index}].role_tag is invalid.")
        result.append({"id": identifier, "role_tag": role_tag})
    return tuple(result)


def _memo_context(context: CallContext | None, request_id: str | None) -> CallContext:
    if context is None:
        return CallContext(
            request_id=request_id, component=COMPONENT, max_tokens=MAX_MEMO_OUTPUT_TOKENS
        )
    if (
        request_id is not None
        and context.request_id is not None
        and request_id != context.request_id
    ):
        raise ValueError("request_id and context.request_id must agree.")
    configured_max = context.max_tokens
    max_tokens = (
        MAX_MEMO_OUTPUT_TOKENS
        if configured_max is None
        else min(configured_max, MAX_MEMO_OUTPUT_TOKENS)
    )
    return replace(
        context,
        request_id=request_id or context.request_id,
        component=context.component or COMPONENT,
        max_tokens=max_tokens,
    )


def _unmask(text: str, token_map: Mapping[str, str]) -> str:
    if not token_map:
        return text
    inverse = sorted(
        ((token, original) for original, token in token_map.items()), key=lambda item: -len(item[0])
    )
    result = text
    for token, original in inverse:

        def replace_token(_match: re.Match[str], replacement: str = original) -> str:
            return replacement

        result = re.sub(
            r"(?<!\w)" + re.escape(token) + r"(?!\w)",
            replace_token,
            result,
        )
    return result


def _retry_prompt(prompt: MaskedPrompt, report: Stage7ShapeReport | None) -> MaskedPrompt:
    detail = "; ".join(report.failures) if report is not None else "the declared shape"
    detail = detail[:_MAX_RETRY_DETAIL_LENGTH]
    constraint = (
        "\n\nA prior draft failed the product shape checks. Treat this as a generated "
        "constraint, not as a fact: regenerate the same JSON shape and correct "
        "only these issues: " + json.dumps(detail, ensure_ascii=False) + ".\n"
    )
    return MaskedPrompt(
        content=prompt.content + constraint,
        version=prompt.version,
        fields=prompt.fields,
        token_map=prompt.token_map,
    )


def _parser_failure_report(report: Stage7ShapeReport, detail: str) -> Stage7ShapeReport:
    # A parser failure is a model-shape failure even if a permissive checker
    # could otherwise find enough fields to pass.  Rebuild only the action
    # check so the report still exposes the same four stable checks.
    return replace(
        report,
        actions=ShapeCheck(
            name="catalogue_actions",
            passed=False,
            failures=("reply shape: " + detail[:_MAX_RETRY_DETAIL_LENGTH],),
        ),
    )
