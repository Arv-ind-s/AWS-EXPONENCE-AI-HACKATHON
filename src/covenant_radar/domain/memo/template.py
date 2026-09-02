"""The fixed, versioned structure of a grounded warning memo.

The template is deliberately a domain value object.  It describes the shape
that the later drafting stage is allowed to fill; it does not render prose or
know anything about a model provider.  Keeping the section and slot contract
here means a prompt, a web view and a persisted memo cannot silently grow
different shapes.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Final

MEMO_TEMPLATE_VERSION: Final[str] = "memo.template.v1"


@dataclass(frozen=True, slots=True)
class MemoSection:
    """One fixed memo section and the slots it owns."""

    name: str
    slot_names: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name.strip():
            raise ValueError("Memo section name must be non-blank text.")
        name = self.name.strip()
        if not isinstance(self.slot_names, tuple):
            object.__setattr__(self, "slot_names", tuple(self.slot_names))
        if any(not isinstance(slot, str) or not slot.strip() for slot in self.slot_names):
            raise ValueError("Memo section slot names must be non-blank text.")
        normalized_slots = tuple(slot.strip() for slot in self.slot_names)
        if len(normalized_slots) != len(set(normalized_slots)):
            raise ValueError(f"Memo section {name!r} contains duplicate slots.")
        object.__setattr__(self, "name", name)
        object.__setattr__(self, "slot_names", normalized_slots)


# These names are the stable section identifiers used by storage and callers.
MEMO_SECTION_NAMES: Final[tuple[str, ...]] = (
    "situation",
    "covenant_position",
    "drivers",
    "evidence_citations",
    "simulated_options",
    "recommended_interventions",
    "advisory_closing",
)

# The advisory closing is fixed policy text, not a model-generated fact.  It
# is intentionally not a slot: only record-backed values enter the slot map.
ADVISORY_CLOSING_TEXT: Final[str] = (
    "This memo is advisory. Human credit review is required before action."
)


@dataclass(frozen=True, slots=True)
class MemoTemplate:
    """A validated memo template with a fixed section topology."""

    version: str
    sections: tuple[MemoSection, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.version, str) or not self.version.strip():
            raise ValueError("Memo template version must be non-blank text.")
        if len(self.version.strip()) > 50:
            raise ValueError("Memo template version must be at most 50 characters.")
        sections = tuple(self.sections)
        if not sections:
            raise ValueError("Memo template must contain at least one section.")
        if any(not isinstance(section, MemoSection) for section in sections):
            raise TypeError("Memo template sections must be MemoSection values.")
        names = tuple(section.name for section in sections)
        if len(names) != len(set(names)):
            raise ValueError("Memo template section names must be unique.")
        object.__setattr__(self, "version", self.version.strip())
        object.__setattr__(self, "sections", sections)

    @property
    def section_names(self) -> tuple[str, ...]:
        """Return the stable section identifiers in display order."""

        return tuple(section.name for section in self.sections)

    @property
    def slot_names(self) -> tuple[str, ...]:
        """Return every slot exactly once in section order."""

        names: list[str] = []
        for section in self.sections:
            for slot_name in section.slot_names:
                if slot_name in names:
                    raise ValueError(f"Memo template slot {slot_name!r} appears more than once.")
                names.append(slot_name)
        return tuple(names)

    def section(self, name: str) -> MemoSection:
        """Return one section by identifier."""

        if not isinstance(name, str) or not name.strip():
            raise ValueError("Memo section name must be non-blank text.")
        normalized = name.strip()
        for section in self.sections:
            if section.name == normalized:
                return section
        raise KeyError(normalized)

    @property
    def is_fixed(self) -> bool:
        """Whether this template is the T-099 topology."""

        return self.version == MEMO_TEMPLATE_VERSION and self.sections == MEMO_SECTIONS


MEMO_SECTIONS: Final[tuple[MemoSection, ...]] = (
    MemoSection("situation", ("situation",)),
    MemoSection(
        "covenant_position",
        (
            "ratio_name",
            "value",
            "threshold",
            "headroom",
            "probability",
            "confidence",
            "crossing_date",
        ),
    ),
    MemoSection("drivers", ("drivers",)),
    MemoSection("evidence_citations", ("evidence_counts",)),
    MemoSection("simulated_options", ("simulation_options",)),
    MemoSection(
        "recommended_interventions",
        ("recommended_interventions", "intervention_text"),
    ),
    MemoSection("advisory_closing", ()),
)

DEFAULT_MEMO_TEMPLATE: Final[MemoTemplate] = MemoTemplate(
    version=MEMO_TEMPLATE_VERSION,
    sections=MEMO_SECTIONS,
)

# Explicit aliases make the contract discoverable to callers without making
# the section topology mutable.
FIXED_MEMO_SECTIONS: Final[tuple[MemoSection, ...]] = MEMO_SECTIONS
TEMPLATE_SECTIONS: Final[tuple[MemoSection, ...]] = MEMO_SECTIONS


__all__ = [
    "ADVISORY_CLOSING_TEXT",
    "DEFAULT_MEMO_TEMPLATE",
    "FIXED_MEMO_SECTIONS",
    "MEMO_SECTIONS",
    "MEMO_SECTION_NAMES",
    "MEMO_TEMPLATE_VERSION",
    "MemoSection",
    "MemoTemplate",
    "TEMPLATE_SECTIONS",
]
