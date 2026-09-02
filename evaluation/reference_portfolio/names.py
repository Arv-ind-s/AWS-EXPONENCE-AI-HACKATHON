"""Stable Indian entity and contact names for the reference portfolio.

No external name corpus is used.  The small, intentionally curated corpus
keeps the evaluation build offline and makes its output reproducible while
still exercising realistic Indian commercial-credit display lengths.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from random import Random
from typing import Final

_CIN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[UL]\d{5}[A-Z]{2}\d{4}[A-Z]{3}\d{6}$")
_PAN_PATTERN: Final[re.Pattern[str]] = re.compile(r"^[A-Z]{5}\d{4}[A-Z]$")

GIVEN_NAMES: Final[tuple[str, ...]] = (
    "Aarav",
    "Aditi",
    "Ananya",
    "Arjun",
    "Devika",
    "Ishaan",
    "Kavya",
    "Meera",
    "Nandini",
    "Rohan",
    "Saanvi",
    "Vikram",
)

SURNAMES: Final[tuple[str, ...]] = (
    "Bhatia",
    "Chatterjee",
    "Deshmukh",
    "Iyer",
    "Kapoor",
    "Malhotra",
    "Menon",
    "Mukherjee",
    "Nair",
    "Patel",
    "Reddy",
    "Sharma",
    "Singh",
    "Subramanian",
    "Varma",
)

BUSINESS_DESCRIPTORS: Final[tuple[str, ...]] = (
    "Advanced Industrial Components",
    "Agro Processing and Logistics",
    "Applied Engineering Systems",
    "Capital Equipment and Services",
    "Commercial Infrastructure Solutions",
    "Consumer Products and Distribution",
    "Energy Transition Technologies",
    "Integrated Manufacturing Systems",
    "National Supply Chain Services",
    "Precision Materials and Components",
    "Renewable Infrastructure Projects",
    "Speciality Chemicals and Technologies",
)

INDIAN_STATE_CODES: Final[tuple[str, ...]] = (
    "AP",
    "DL",
    "GJ",
    "KA",
    "KL",
    "MH",
    "MP",
    "PB",
    "RJ",
    "TN",
    "TS",
    "WB",
)

CONTACT_DESIGNATIONS: Final[tuple[str, ...]] = (
    "Chief Financial Officer",
    "Company Secretary",
    "Finance Controller",
    "Treasury Head",
)


def build_legal_name(random_source: Random, sequence: int) -> str:
    """Return a deterministic, unique-length legal entity name."""
    if sequence < 1:
        raise ValueError("A legal-name sequence must be positive.")
    director = random_source.choice(GIVEN_NAMES)
    surname = random_source.choice(SURNAMES)
    descriptor = random_source.choice(BUSINESS_DESCRIPTORS)
    # The sequence is part of the name, rather than only the reference, so a
    # generated book never relies on accidental uniqueness in the corpus.
    return f"{surname} {descriptor} {director} {sequence:05d} Private Limited"


def build_contact_name(random_source: Random) -> str:
    """Return a synthetic contact name with a realistic display length."""
    return f"{random_source.choice(GIVEN_NAMES)} {random_source.choice(SURNAMES)}"


def build_cin(sequence: int, random_source: Random) -> str:
    """Build a syntactically valid, deterministic Corporate Identity Number."""
    if not 1 <= sequence <= 99_999:
        raise ValueError("CIN sequence must be between 1 and 99999.")
    listing = random_source.choice(("U", "L"))
    state = random_source.choice(INDIAN_STATE_CODES)
    incorporation_year = 2000 + (sequence % 26)
    registration = f"{sequence:06d}"
    return f"{listing}{sequence:05d}{state}{incorporation_year:04d}PTC{registration}"


def build_pan(sequence: int, random_source: Random) -> str:
    """Build a syntactically valid synthetic PAN-shaped identifier."""
    if sequence < 1:
        raise ValueError("PAN sequence must be positive.")
    letters = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
    value = sequence - 1
    prefix: list[str] = []
    for _ in range(5):
        value, remainder = divmod(value, len(letters))
        prefix.append(letters[remainder])
    category = random_source.choice(letters)
    return f"{''.join(prefix)}{sequence % 10_000:04d}{category}"


def is_valid_cin(value: str) -> bool:
    """Return whether *value* matches the documented CIN shape."""
    return bool(_CIN_PATTERN.fullmatch(value))


def is_valid_pan(value: str) -> bool:
    """Return whether *value* matches the documented PAN shape."""
    return bool(_PAN_PATTERN.fullmatch(value))


NameFactory = Callable[[Random, int], str]

__all__ = [
    "BUSINESS_DESCRIPTORS",
    "CONTACT_DESIGNATIONS",
    "GIVEN_NAMES",
    "INDIAN_STATE_CODES",
    "NameFactory",
    "SURNAMES",
    "build_contact_name",
    "build_cin",
    "build_legal_name",
    "build_pan",
    "is_valid_cin",
    "is_valid_pan",
]
