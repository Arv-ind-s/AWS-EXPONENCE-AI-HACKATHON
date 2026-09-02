"""Render shape for the warning-memo block (`C-08`).

`MemoGenerationService.generate` returns one of four outcomes and never
raises for a refusal, a provider outage or a call ceiling.  This module turns
each of them — plus the "there is nothing to ground a memo in" case the route
detects before calling the service at all — into one block the borrower screen
can swap in, so the rest of the case file keeps rendering whatever happened
(`spec §R-17.c`).

Model prose is carried in named fields and labelled, never merged into the
surrounding page text: a reader must be able to see where the drafted words
start and stop.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Final
from uuid import UUID

from covenant_radar.ai.memo import MemoDraft
from covenant_radar.db.models.workflow import Memo
from covenant_radar.services.memo import MemoGenerationOutcome, MemoOutcomeKind

NO_FORECAST_MESSAGE: Final[str] = (
    "No completed forecast is recorded for this borrower, so there is nothing to ground a "
    "memo in yet. A memo becomes available after the next scored run."
)
NOT_CONFIGURED_MESSAGE: Final[str] = (
    "Memo drafting is unavailable because no model provider is configured for this "
    "deployment. Everything else on this screen is unaffected."
)

_TITLES: Final[dict[str, str]] = {
    "generated": "AI-generated explanation",
    "refused": "No memo was produced",
    "degraded": "Memo drafting is unavailable",
    "queued": "Memo request queued",
    "unavailable": "Memo not available",
}


class MemoBlockState(StrEnum):
    """The five states the memo block can render."""

    GENERATED = "generated"
    REFUSED = "refused"
    DEGRADED = "degraded"
    QUEUED = "queued"
    UNAVAILABLE = "unavailable"


@dataclass(frozen=True, slots=True)
class MemoActionView:
    """One catalogue action the draft selected, with its owning role."""

    id: str
    role_tag: str
    text: str


@dataclass(frozen=True, slots=True)
class MemoCitationView:
    """One persisted record used to ground an AI-drafted memo slot."""

    label: str
    source_type: str
    source_id: str
    href: str | None = None


@dataclass(frozen=True, slots=True)
class MemoBlockView:
    """One rendered memo outcome.

    Only ``GENERATED`` carries prose. The other four carry a message that
    explains, in the reader's terms, why there is no memo — the refusal case
    additionally naming the checks that failed, because `spec §R-17.b` keeps
    "why is there no memo" answerable even though no row was written.
    """

    state: MemoBlockState
    title: str
    message: str = ""
    memo_id: UUID | None = None
    label: str = ""
    headline: str = ""
    summary: str = ""
    drivers: tuple[str, ...] = ()
    actions: tuple[MemoActionView, ...] = ()
    recommended_next_step: str = ""
    disclaimer: str = ""
    provider: str = ""
    model_version: str = ""
    prompt_version: str = ""
    check_verdict: str = ""
    citations: tuple[MemoCitationView, ...] = ()
    failed_checks: tuple[str, ...] = ()
    retry_at: datetime | None = None
    dimension: str | None = None

    def __post_init__(self) -> None:
        state = MemoBlockState(self.state)
        object.__setattr__(self, "state", state)
        object.__setattr__(self, "drivers", tuple(self.drivers))
        object.__setattr__(self, "actions", tuple(self.actions))
        object.__setattr__(self, "citations", tuple(self.citations))
        object.__setattr__(self, "failed_checks", tuple(self.failed_checks))
        if state is MemoBlockState.GENERATED:
            if not self.headline.strip() or not self.summary.strip():
                raise ValueError("A generated memo block requires its drafted prose.")
            if not isinstance(self.memo_id, UUID):
                raise ValueError("A generated memo block requires its persisted memo id.")
            if not self.provider.strip() or not self.model_version.strip():
                raise ValueError("A generated memo block requires provider and model provenance.")
            if not self.prompt_version.strip() or not self.check_verdict.strip():
                raise ValueError("A generated memo block requires prompt and check provenance.")
        elif not self.message.strip():
            raise ValueError(f"A {state.value} memo block requires an explanatory message.")
        if state is not MemoBlockState.REFUSED and self.failed_checks:
            raise ValueError("Only a refused memo block names failed checks.")
        if state is not MemoBlockState.QUEUED and (
            self.retry_at is not None or self.dimension is not None
        ):
            raise ValueError("Only a queued memo block carries retry_at or dimension.")

    @property
    def generated(self) -> bool:
        return self.state is MemoBlockState.GENERATED


def build_memo_block(outcome: MemoGenerationOutcome) -> MemoBlockView:
    """Convert one generation outcome into its block."""

    if not isinstance(outcome, MemoGenerationOutcome):
        raise TypeError("build_memo_block requires a MemoGenerationOutcome.")

    memo = outcome.memo
    drafting = outcome.drafting
    if outcome.kind is MemoOutcomeKind.GENERATED and memo is not None and drafting is not None:
        return _generated(memo, drafting.draft)
    if outcome.kind is MemoOutcomeKind.REFUSED:
        return MemoBlockView(
            state=MemoBlockState.REFUSED,
            title=_TITLES["refused"],
            message=outcome.message or "",
            failed_checks=outcome.failed_checks,
        )
    if outcome.kind is MemoOutcomeKind.PROVIDER_UNAVAILABLE:
        return MemoBlockView(
            state=MemoBlockState.DEGRADED,
            title=_TITLES["degraded"],
            message=outcome.message or "",
        )
    return MemoBlockView(
        state=MemoBlockState.QUEUED,
        title=_TITLES["queued"],
        message=outcome.message or "",
        retry_at=outcome.retry_at,
        dimension=outcome.dimension,
    )


def memo_unavailable(message: str) -> MemoBlockView:
    """Build the block for a memo the route declined to attempt."""

    return MemoBlockView(
        state=MemoBlockState.UNAVAILABLE,
        title=_TITLES["unavailable"],
        message=message,
    )


def build_persisted_memo_block(memo: Memo) -> MemoBlockView:
    """Rebuild the display block for a previously generated memo.

    Generated prose is persisted in four fixed paragraphs while its drivers
    and catalogue actions remain in the grounded slot map.  Rehydrating those
    fields here keeps a refreshed borrower page equivalent to the response
    that was swapped in immediately after generation.
    """

    if not isinstance(memo, Memo):
        raise TypeError("build_persisted_memo_block requires a Memo.")
    prose = tuple(part.strip() for part in memo.drafted_text.split("\n\n", 3))
    if len(prose) != 4 or any(not part for part in prose):
        raise ValueError("A persisted generated memo must contain four prose sections.")

    slot_values = _persisted_slot_values(memo.slots)
    drivers = tuple(
        str(item.get("name", "")).strip()
        for item in _mapping_items(slot_values.get("drivers"))
        if str(item.get("name", "")).strip()
    )
    recommended = {
        str(item.get("code", "")): item
        for item in _mapping_items(slot_values.get("recommended_interventions"))
    }
    actions = tuple(
        MemoActionView(
            id=str(item.get("id", "")),
            role_tag=str(item.get("role_tag", "")),
            text=str(recommended.get(str(item.get("id", "")), {}).get("text", "")),
        )
        for item in _mapping_items((memo.actions or {}).get("items"))
    )
    return MemoBlockView(
        state=MemoBlockState.GENERATED,
        title=_TITLES["generated"],
        memo_id=memo.id,
        label="Drafted by model",
        headline=_trim_drafted_numbers(prose[0]),
        summary=_trim_drafted_numbers(prose[1]),
        drivers=tuple(_trim_drafted_numbers(driver) for driver in drivers),
        actions=actions,
        recommended_next_step=_trim_drafted_numbers(prose[2]),
        disclaimer=_trim_drafted_numbers(prose[3]),
        provider=_provider_label(memo.provider),
        model_version=memo.model_version or "Not recorded",
        prompt_version=memo.prompt_version or "Not recorded",
        check_verdict=_verdict_label(memo.check_verdict),
        citations=_persisted_citations(memo.slots),
    )


def _persisted_slot_values(slots: Mapping[str, object]) -> Mapping[str, object]:
    values = slots.get("slots")
    if not isinstance(values, Mapping):
        return {}
    return {
        str(name): value.get("value")
        for name, value in values.items()
        if isinstance(value, Mapping)
    }


def _mapping_items(value: object) -> tuple[Mapping[str, object], ...]:
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item for item in value if isinstance(item, Mapping))


def _persisted_citations(slots: Mapping[str, object]) -> tuple[MemoCitationView, ...]:
    raw_slots = slots.get("slots")
    if not isinstance(raw_slots, Mapping):
        return ()
    citations: list[MemoCitationView] = []
    seen: set[tuple[str, str]] = set()
    for raw_slot in raw_slots.values():
        if not isinstance(raw_slot, Mapping):
            continue
        for reference in _mapping_items(raw_slot.get("record_references")):
            source_type = str(reference.get("type", "")).strip()
            source_id = str(reference.get("id", "")).strip()
            key = (source_type, source_id)
            if not source_type or not source_id or key in seen:
                continue
            seen.add(key)
            citations.append(
                MemoCitationView(
                    label=f"{_source_label(source_type)} — {source_id}",
                    source_type=source_type,
                    source_id=source_id,
                    href=_source_href(source_type, source_id),
                )
            )
    return tuple(citations)


_LONG_DECIMAL_RE: Final[re.Pattern[str]] = re.compile(r"(?<!\d)(\d+\.\d{4,})(?!\d)")


def _trim_drafted_numbers(text: str) -> str:
    """Shorten a drafted sentence's over-precise decimals for display.

    A grounding slot reaches the model at its stored database precision — a
    `RatioValue` column is scale 8 — so a drafted sentence that repeats one
    verbatim reads as "1.76000000" instead of "1.76". This trims it at
    render time, after the model has already replied: the fix cannot live
    upstream in the prompt without changing the exact bytes cassette replay
    matches on (`ai/providers/recorded.py`'s `cassette_key` hashes the exact
    masked messages), which would turn every already-recorded memo into a
    cassette miss instead of a clean number.
    """

    def _replace(match: re.Match[str]) -> str:
        try:
            number = Decimal(match.group(1))
        except InvalidOperation:
            return match.group(1)
        text = format(number.quantize(Decimal("0.0001")), "f")
        return text.rstrip("0").rstrip(".") if "." in text else text

    return _LONG_DECIMAL_RE.sub(_replace, text)


def _provider_label(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Not recorded"
    provider = value.strip()
    return "Covenant Radar AI" if provider.lower() == "tcs" else provider


def _verdict_label(value: str | None) -> str:
    if not isinstance(value, str) or not value.strip():
        return "Not recorded"
    return value.strip().replace("_", " ").capitalize()


def _source_label(source_type: str) -> str:
    return {
        "triage_entry": "Triage decision",
        "forecast": "Forecast",
        "forecast_driver": "Forecast driver",
        "evidence_item": "Evidence item",
        "simulation": "Simulation",
        "intervention": "Bank action catalogue",
    }.get(source_type, source_type.replace("_", " ").capitalize())


def _source_href(source_type: str, source_id: str) -> str | None:
    if source_type == "forecast":
        return f"/why/forecast/{source_id}"
    if source_type == "evidence_item":
        return f"#evidence-item-{source_id}"
    if source_type == "simulation":
        return f"/api/v1/simulations/{source_id}"
    return None


def _generated(memo: Memo, draft: MemoDraft) -> MemoBlockView:
    slot_values = _persisted_slot_values(memo.slots)
    recommended = {
        str(item.get("code", "")): item
        for item in _mapping_items(slot_values.get("recommended_interventions"))
    }
    return MemoBlockView(
        state=MemoBlockState.GENERATED,
        title=_TITLES["generated"],
        memo_id=memo.id,
        label=draft.label,
        headline=_trim_drafted_numbers(draft.headline),
        summary=_trim_drafted_numbers(draft.summary),
        drivers=tuple(_trim_drafted_numbers(driver) for driver in draft.drivers),
        actions=tuple(
            MemoActionView(
                id=str(action.get("id", "")),
                role_tag=str(action.get("role_tag", "")),
                # The strict model reply deliberately contains only action
                # identifiers and role tags. The human-readable catalogue
                # text is a grounded input, so restore it from the persisted
                # slot map for the immediate HTMX response just as the reload
                # path does. Previously this column stayed blank until reload.
                text=str(recommended.get(str(action.get("id", "")), {}).get("text", "")),
            )
            for action in draft.actions
        ),
        recommended_next_step=_trim_drafted_numbers(draft.recommended_next_step),
        disclaimer=_trim_drafted_numbers(draft.disclaimer),
        provider=_provider_label(memo.provider),
        model_version=memo.model_version or "Not recorded",
        prompt_version=memo.prompt_version or "Not recorded",
        check_verdict=_verdict_label(memo.check_verdict),
        citations=_persisted_citations(memo.slots),
    )


__all__ = [
    "NOT_CONFIGURED_MESSAGE",
    "NO_FORECAST_MESSAGE",
    "MemoActionView",
    "MemoBlockState",
    "MemoBlockView",
    "MemoCitationView",
    "build_memo_block",
    "build_persisted_memo_block",
    "memo_unavailable",
]
