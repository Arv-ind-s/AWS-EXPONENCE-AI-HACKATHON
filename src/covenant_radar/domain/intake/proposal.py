"""Stage-1 proposal parsing and normalisation (`spec §17.1`, `plan.md §8`'s
`T-094`).

The model proposes a hypothesis about one clause candidate; it decides
nothing. This module owns exactly two responsibilities and no more:

* parse the model's reply **strictly** against the declared output shape
  (`ai/prompts/stage1_extract.v1.md`) — anything that does not match that
  shape produces an unparseable proposal, never a partially trusted one; and
* normalise a strictly-parsed reply's fields into the shapes `T-095` checks —
  a threshold as a `Decimal`, a direction and frequency in their
  enumerations, a date resolved to the bank's FY calendar — flagging a field
  as ambiguous rather than guessing at it.

No refusal decision is made here. A definition outside the ratio library, an
inconsistent unit, an implausible threshold and an effective date before the
facility's sanction are all carried through unexamined for `T-095` to
independently disprove; that separation — proposal versus verification — is
the whole point of the two-stage design.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Final

from covenant_radar.core.errors import ValidationError
from covenant_radar.domain.intake.candidates import ClauseCandidate
from covenant_radar.i18n.formatting import format_fy_label

__all__ = [
    "DIRECTION_WORDS",
    "FREQUENCY_WORDS",
    "UNIT_KINDS",
    "ProposalShapeError",
    "StageOneProposal",
    "parse_stage1_reply",
]

# The output shape `ai/prompts/stage1_extract.v1.md` declares, verbatim.
_REQUIRED_KEYS: Final[frozenset[str]] = frozenset(
    {
        "definition",
        "custom_formula",
        "threshold",
        "direction",
        "unit",
        "currency",
        "frequency",
        "effective_from",
        "effective_to",
        "exceptions",
        "cure_period_days",
        "source_quote",
    }
)

#: The prompt's own vocabulary for `unit` — deliberately its own small set,
#: not `domain.covenants.model.UNITS`: whether a proposed unit is even
#: consistent with the named ratio's actual unit is `T-095`'s check, not a
#: fact this module can establish from the reply alone.
UNIT_KINDS: Final[frozenset[str]] = frozenset({"ratio", "percent", "currency", "days", "count"})

#: The prompt's own vocabulary for `direction` — the side of the threshold a
#: clause's plain language names, mapped below to `domain.covenants.model`'s
#: `min`/`max` breach-direction vocabulary.
DIRECTION_WORDS: Final[frozenset[str]] = frozenset({"above", "below"})

#: The prompt's own vocabulary for `frequency`, mapped below to
#: `domain.covenants.model.FREQUENCIES`.
FREQUENCY_WORDS: Final[frozenset[str]] = frozenset(
    {"monthly", "quarterly", "half_yearly", "yearly", "event_driven"}
)

#: `direction_hint="min"` (`domain/ratios/definitions.py`) means breach is
#: falling *below* the threshold, i.e. the covenant requires the value to
#: stay at or *above* it — the clause language `stage1_extract.v1.md` asks
#: the model to render as ``"above"``. `direction_hint="max"` is the mirror:
#: the covenant requires the value to stay at or *below* the threshold,
#: rendered as ``"below"``.
_DIRECTION_WORD_TO_ENUM: Final[dict[str, str]] = {"above": "min", "below": "max"}

_FREQUENCY_WORD_TO_ENUM: Final[dict[str, str]] = {
    "monthly": "monthly",
    "quarterly": "quarterly",
    "half_yearly": "half_yearly",
    "yearly": "annual",
    "event_driven": "on_event",
}

_MAX_REPLY_BYTES: Final[int] = 65_536
_MAX_TEXT_FIELD_LENGTH: Final[int] = 4_000
_MAX_EXCEPTIONS: Final[int] = 32
_MAX_CURE_PERIOD_DAYS: Final[int] = 3_650
_CURRENCY_CODE_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z]{3}")
_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")

# A raw threshold's free text, after a currency prefix and thousands
# separators are stripped, must reduce to exactly one signed number and, at
# most, one recognised unit suffix — anything left over (a hedge word, a
# range, prose) means the figure cannot be normalised without guessing.
_CURRENCY_PREFIX_RE: Final[re.Pattern[str]] = re.compile(r"^(?:₹|rs\.?|inr)\s*", re.IGNORECASE)
_THRESHOLD_TEXT_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?P<num>-?\d+(?:\.\d+)?)\s*(?P<suffix>%|percent|x|times|crore|cr|lakh|lac|days?)?$",
    re.IGNORECASE,
)
_LAKH_SUFFIXES: Final[frozenset[str]] = frozenset({"lakh", "lac"})

# A light cross-check between the model's chosen `frequency` and its own
# supporting quote: text naming more than one of these categories is
# genuinely ambiguous evidence, and `T-094` flags it rather than picking one
# on the model's behalf. Deliberately narrow — no attempt is made to decide
# whether the *chosen* value agrees with the quote, only whether the quote
# itself names more than one period.
_FREQUENCY_KEYWORDS: Final[dict[str, tuple[re.Pattern[str], ...]]] = {
    "monthly": (re.compile(r"\bmonthly\b|\beach month\b", re.IGNORECASE),),
    "quarterly": (re.compile(r"\bquarterly\b|\beach quarter\b", re.IGNORECASE),),
    "half_yearly": (
        re.compile(r"\bhalf[\s-]*yearly\b|\bsemi[\s-]*annual(?:ly)?\b", re.IGNORECASE),
    ),
    "annual": (re.compile(r"\bannual(?:ly)?\b|\byearly\b", re.IGNORECASE),),
    "on_event": (
        re.compile(
            r"\bevent[\s-]*driven\b|\bon\s+occurrence\b|\bupon\s+(?:the\s+)?occurrence\b",
            re.IGNORECASE,
        ),
    ),
}


class ProposalShapeError(ValidationError):
    """The model's reply does not match the declared stage-1 output shape.

    Caught internally by :func:`parse_stage1_reply`; a caller never sees
    this raised — it becomes ``StageOneProposal.parse_error`` instead, so a
    malformed reply is reported, never partially trusted.
    """

    code = "stage1_proposal_unparseable"


@dataclass(frozen=True, slots=True)
class StageOneProposal:
    """One candidate's stage-1 outcome: either unparseable, with the raw
    reply and a parse error and nothing else, or fully normalised.

    ``raw_reply`` is retained in both cases, unmodified, for the trace.
    Every other field defaults to its "nothing proposed" value so an
    unparseable proposal cannot accidentally carry normalised data forward.
    """

    candidate: ClauseCandidate
    parseable: bool
    raw_reply: str
    parse_error: str | None = None
    definition_ref: str | None = None
    custom_formula: str | None = None
    threshold: Decimal | None = None
    threshold_ambiguous: bool = False
    unit: str | None = None
    currency: str | None = None
    direction: str | None = None
    frequency: str | None = None
    frequency_ambiguous: bool = False
    effective_from: date | None = None
    effective_from_fy_label: str | None = None
    effective_to: date | None = None
    effective_to_fy_label: str | None = None
    exceptions: tuple[str, ...] = ()
    cure_period_days: int | None = None
    source_quote: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.candidate, ClauseCandidate):
            raise TypeError("StageOneProposal.candidate must be a ClauseCandidate.")
        if not isinstance(self.raw_reply, str):
            raise TypeError("StageOneProposal.raw_reply must be text.")
        object.__setattr__(self, "exceptions", tuple(self.exceptions))
        if not self.parseable:
            if not self.parse_error:
                raise ValueError("An unparseable StageOneProposal must carry a parse_error.")
            normalised_fields = (
                self.definition_ref,
                self.custom_formula,
                self.threshold,
                self.unit,
                self.currency,
                self.direction,
                self.frequency,
                self.effective_from,
                self.effective_from_fy_label,
                self.effective_to,
                self.effective_to_fy_label,
                self.cure_period_days,
                self.source_quote,
            )
            if any(value is not None for value in normalised_fields) or self.exceptions:
                raise ValueError(
                    "An unparseable StageOneProposal must not carry normalised field values."
                )
            if self.threshold_ambiguous or self.frequency_ambiguous:
                raise ValueError(
                    "An unparseable StageOneProposal must not carry ambiguity flags either."
                )
            return
        if self.parse_error is not None:
            raise ValueError("A parseable StageOneProposal must not carry a parse_error.")
        if self.source_quote is None or not self.source_quote.strip():
            raise ValueError("A parseable StageOneProposal must carry a non-empty source_quote.")


@dataclass(frozen=True, slots=True)
class _NormalisedFields:
    """The strictly-typed result of :func:`_normalise`, split out from
    :class:`StageOneProposal` so the constructor call in
    :func:`parse_stage1_reply` never needs an untyped ``**dict`` splat."""

    definition_ref: str | None
    custom_formula: str | None
    threshold: Decimal | None
    threshold_ambiguous: bool
    unit: str | None
    currency: str | None
    direction: str | None
    frequency: str | None
    frequency_ambiguous: bool
    effective_from: date | None
    effective_from_fy_label: str | None
    effective_to: date | None
    effective_to_fy_label: str | None
    exceptions: tuple[str, ...]
    cure_period_days: int | None
    source_quote: str


def parse_stage1_reply(candidate: ClauseCandidate, raw_reply: str) -> StageOneProposal:
    """Parse and normalise one candidate's stage-1 model reply.

    Never raises for a malformed reply: every failure short of a caller
    programming error (a bad `candidate` or a non-text `raw_reply`) is
    captured as ``StageOneProposal.parse_error`` instead, with the raw reply
    still retained for the trace.
    """

    if not isinstance(candidate, ClauseCandidate):
        raise TypeError("parse_stage1_reply requires a ClauseCandidate.")
    if not isinstance(raw_reply, str):
        raise TypeError("parse_stage1_reply requires the raw reply as text.")

    try:
        payload = _parse_shape(raw_reply)
        normalised = _normalise(payload)
    except ProposalShapeError as error:
        return StageOneProposal(
            candidate=candidate,
            parseable=False,
            raw_reply=raw_reply,
            parse_error=str(error),
        )
    return StageOneProposal(
        candidate=candidate,
        parseable=True,
        raw_reply=raw_reply,
        definition_ref=normalised.definition_ref,
        custom_formula=normalised.custom_formula,
        threshold=normalised.threshold,
        threshold_ambiguous=normalised.threshold_ambiguous,
        unit=normalised.unit,
        currency=normalised.currency,
        direction=normalised.direction,
        frequency=normalised.frequency,
        frequency_ambiguous=normalised.frequency_ambiguous,
        effective_from=normalised.effective_from,
        effective_from_fy_label=normalised.effective_from_fy_label,
        effective_to=normalised.effective_to,
        effective_to_fy_label=normalised.effective_to_fy_label,
        exceptions=normalised.exceptions,
        cure_period_days=normalised.cure_period_days,
        source_quote=normalised.source_quote,
    )


def _parse_shape(raw_reply: str) -> dict[str, object]:
    if len(raw_reply.encode("utf-8")) > _MAX_REPLY_BYTES:
        raise ProposalShapeError(f"Stage-1 reply exceeds the {_MAX_REPLY_BYTES}-byte limit.")
    try:
        payload = json.loads(raw_reply)
    except json.JSONDecodeError as error:
        raise ProposalShapeError(f"Stage-1 reply is not valid JSON: {error}.") from error
    if not isinstance(payload, dict):
        raise ProposalShapeError("Stage-1 reply must be a single JSON object.")

    keys = set(payload)
    missing = sorted(_REQUIRED_KEYS - keys)
    extra = sorted(keys - _REQUIRED_KEYS)
    if missing or extra:
        parts = []
        if missing:
            parts.append(f"missing {', '.join(missing)}")
        if extra:
            parts.append(f"unexpected {', '.join(extra)}")
        raise ProposalShapeError(
            f"Stage-1 reply does not match the declared shape: {'; '.join(parts)}."
        )

    validated: dict[str, object] = {
        "definition": _definition_field(payload["definition"]),
        "custom_formula": _optional_text(payload["custom_formula"], "custom_formula"),
        "threshold": _threshold_field(payload["threshold"]),
        "direction": _enum_or_none(payload["direction"], "direction", DIRECTION_WORDS),
        "unit": _enum_or_none(payload["unit"], "unit", UNIT_KINDS),
        "currency": _currency_field(payload["currency"]),
        "frequency": _enum_or_none(payload["frequency"], "frequency", FREQUENCY_WORDS),
        "effective_from": _date_field(payload["effective_from"], "effective_from"),
        "effective_to": _date_field(payload["effective_to"], "effective_to"),
        "exceptions": _exceptions_field(payload["exceptions"]),
        "cure_period_days": _cure_period_field(payload["cure_period_days"]),
        "source_quote": _required_text(payload["source_quote"], "source_quote"),
    }
    return validated


def _normalise(payload: dict[str, object]) -> _NormalisedFields:
    raw_threshold = payload["threshold"]
    threshold, threshold_ambiguous = _normalise_threshold(raw_threshold)

    raw_direction = payload["direction"]
    direction = _DIRECTION_WORD_TO_ENUM[raw_direction] if isinstance(raw_direction, str) else None

    source_quote = payload["source_quote"]
    assert isinstance(source_quote, str)
    frequency_ambiguous = _frequency_ambiguous(source_quote)
    raw_frequency = payload["frequency"]
    frequency = (
        None
        if frequency_ambiguous or not isinstance(raw_frequency, str)
        else _FREQUENCY_WORD_TO_ENUM[raw_frequency]
    )

    effective_from = payload["effective_from"]
    effective_to = payload["effective_to"]
    assert effective_from is None or isinstance(effective_from, date)
    assert effective_to is None or isinstance(effective_to, date)

    definition_ref = payload["definition"]
    custom_formula = payload["custom_formula"]
    unit = payload["unit"]
    currency = payload["currency"]
    exceptions = payload["exceptions"]
    cure_period_days = payload["cure_period_days"]
    assert definition_ref is None or isinstance(definition_ref, str)
    assert custom_formula is None or isinstance(custom_formula, str)
    assert unit is None or isinstance(unit, str)
    assert currency is None or isinstance(currency, str)
    assert isinstance(exceptions, tuple)
    assert cure_period_days is None or isinstance(cure_period_days, int)

    return _NormalisedFields(
        definition_ref=definition_ref,
        custom_formula=custom_formula,
        threshold=threshold,
        threshold_ambiguous=threshold_ambiguous,
        unit=unit,
        currency=currency,
        direction=direction,
        frequency=frequency,
        frequency_ambiguous=frequency_ambiguous,
        effective_from=effective_from,
        effective_from_fy_label=(
            None if effective_from is None else format_fy_label(effective_from)
        ),
        effective_to=effective_to,
        effective_to_fy_label=None if effective_to is None else format_fy_label(effective_to),
        exceptions=exceptions,
        cure_period_days=cure_period_days,
        source_quote=source_quote,
    )


def _normalise_threshold(raw: object) -> tuple[Decimal | None, bool]:
    if raw is None:
        return None, False
    if isinstance(raw, int | float):
        return Decimal(str(raw)), False
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None, False
        return _parse_threshold_text(text)
    raise TypeError("threshold must already be shape-validated to str, int, float or None.")


def _parse_threshold_text(text: str) -> tuple[Decimal | None, bool]:
    cleaned = _CURRENCY_PREFIX_RE.sub("", text).replace(",", "").strip()
    match = _THRESHOLD_TEXT_RE.fullmatch(cleaned)
    if match is None:
        return None, True
    try:
        value = Decimal(match.group("num"))
    except InvalidOperation:
        return None, True
    suffix = (match.group("suffix") or "").lower()
    if suffix in _LAKH_SUFFIXES:
        value = value / Decimal(100)
    return value, False


def _frequency_ambiguous(source_quote: str) -> bool:
    matched = {
        category
        for category, patterns in _FREQUENCY_KEYWORDS.items()
        if any(pattern.search(source_quote) for pattern in patterns)
    }
    return len(matched) > 1


def _optional_text(value: object, field_name: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ProposalShapeError(f"Stage-1 reply field {field_name!r} must be text or null.")
    cleaned = value.strip()
    if not cleaned:
        return None
    if len(cleaned) > _MAX_TEXT_FIELD_LENGTH:
        raise ProposalShapeError(f"Stage-1 reply field {field_name!r} exceeds the length limit.")
    return cleaned


#: The namespace `domain/intake/candidates.py` gives its ratio-name detection
#: rules (`ratio:leverage_ratio`).  Those rule ids are rendered into the
#: stage-1 prompt as the "detection rules that selected this clause", and a
#: model reliably echoes the prefixed id back as `definition` rather than the
#: bare library key the ratio library is actually keyed by.  Stripping our own
#: prefix here de-aliases an identifier this system minted; it is not a guess
#: about what the model meant, and the de-prefixed name still has to survive
#: `T-095`'s DEFINITION_KNOWN check against the library.
_RULE_ID_DEFINITION_PREFIX: Final[str] = "ratio:"


def _definition_field(value: object) -> str | None:
    cleaned = _optional_text(value, "definition")
    if cleaned is None:
        return None
    if cleaned.startswith(_RULE_ID_DEFINITION_PREFIX):
        return cleaned[len(_RULE_ID_DEFINITION_PREFIX) :].strip() or None
    return cleaned


def _required_text(value: object, field_name: str) -> str:
    cleaned = _optional_text(value, field_name)
    if cleaned is None:
        raise ProposalShapeError(
            f"Stage-1 reply field {field_name!r} is required and must not be blank."
        )
    return cleaned


def _enum_or_none(value: object, field_name: str, allowed: frozenset[str]) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or value not in allowed:
        options = ", ".join(sorted(allowed))
        raise ProposalShapeError(
            f"Stage-1 reply field {field_name!r} must be one of {options} or null."
        )
    return value


def _threshold_field(value: object) -> str | int | float | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProposalShapeError("Stage-1 reply field 'threshold' must be numeric text or null.")
    if isinstance(value, int | float):
        return value
    if isinstance(value, str):
        if len(value) > _MAX_TEXT_FIELD_LENGTH:
            raise ProposalShapeError("Stage-1 reply field 'threshold' exceeds the length limit.")
        return value
    raise ProposalShapeError("Stage-1 reply field 'threshold' must be numeric text or null.")


def _currency_field(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _CURRENCY_CODE_RE.fullmatch(value):
        raise ProposalShapeError(
            "Stage-1 reply field 'currency' must be a 3-letter ISO code or null."
        )
    return value.upper()


def _date_field(value: object, field_name: str) -> date | None:
    if value is None:
        return None
    if not isinstance(value, str) or not _ISO_DATE_RE.fullmatch(value):
        raise ProposalShapeError(f"Stage-1 reply field {field_name!r} must be an ISO date or null.")
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise ProposalShapeError(
            f"Stage-1 reply field {field_name!r} is not a valid date."
        ) from error


def _exceptions_field(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ProposalShapeError("Stage-1 reply field 'exceptions' must be an array.")
    if len(value) > _MAX_EXCEPTIONS:
        raise ProposalShapeError(
            f"Stage-1 reply field 'exceptions' exceeds {_MAX_EXCEPTIONS} entries."
        )
    cleaned: list[str] = []
    for index, item in enumerate(value):
        if not isinstance(item, str) or not item.strip():
            raise ProposalShapeError(
                f"Stage-1 reply field 'exceptions[{index}]' must be non-empty text."
            )
        if len(item) > _MAX_TEXT_FIELD_LENGTH:
            raise ProposalShapeError(
                f"Stage-1 reply field 'exceptions[{index}]' exceeds the length limit."
            )
        cleaned.append(item.strip())
    return tuple(cleaned)


def _cure_period_field(value: object) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ProposalShapeError(
            "Stage-1 reply field 'cure_period_days' must be an integer or null."
        )
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if not isinstance(value, int) or value < 0 or value > _MAX_CURE_PERIOD_DAYS:
        raise ProposalShapeError(
            "Stage-1 reply field 'cure_period_days' must be a non-negative integer or null."
        )
    return value
