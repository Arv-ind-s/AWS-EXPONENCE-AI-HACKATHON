"""Stable fictional entity and contact names for the reference portfolio.

The v1 reference book deliberately favoured obvious synthetic names.  That
made automated fixtures easy to recognise, but produced distracting legal
names such as ``Surname Business Description Firstname 00001 Private
Limited`` on every product surface.  The v2 corpus keeps the useful properties
of the old generator (offline, deterministic and non-identifying) while using
natural-looking invented brands and sector-aligned legal names.

The generation algorithm is intentionally versioned by its frozen tuples.
Changing their order would rename borrowers, so additions belong in a future
version rather than being inserted into these collections.
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

# Invented brand roots.  None is intended to identify a real customer or
# public company.  The first 120 reference borrowers use these roots once each
# with a sector-specific descriptor; the remainder reuse a root only with a
# different descriptor.  This gives 5,000 readable, unique names without
# leaking a sequence number into the legal name.
FICTIONAL_BRAND_ROOTS_V2: Final[tuple[str, ...]] = (
    "Alderwyn",
    "Amberwick",
    "Anviora",
    "Arclume",
    "Ardentia",
    "Arkenvale",
    "Ashmere",
    "Avenrock",
    "Bellora",
    "Bluehaven",
    "Brambletide",
    "Brindale",
    "Cairnwell",
    "Caldera",
    "Cedarwyn",
    "Cinderbay",
    "Cloudmere",
    "Coralstone",
    "Crestora",
    "Dalespring",
    "Dawnridge",
    "Deepwell",
    "Driftwood",
    "Eastmere",
    "Elaris",
    "Emberfield",
    "Everoak",
    "Fablecrest",
    "Fairwind",
    "Fernwick",
    "Flintora",
    "Foxmere",
    "Glenvara",
    "Goldleaf",
    "Greenharbor",
    "Greyhaven",
    "Halewood",
    "Hearthstone",
    "Highmere",
    "Indivar",
    "Ironvale",
    "Ivorybrook",
    "Jadecrest",
    "Junewell",
    "Kalpstone",
    "Kestrelbay",
    "Lakewyn",
    "Larkspur",
    "Lightmere",
    "Loomstone",
    "Lumora",
    "Mapleford",
    "Maravelle",
    "Meadowark",
    "Merrowin",
    "Moonridge",
    "Naviora",
    "Nexvale",
    "Northgrove",
    "Oakspire",
    "Oceanwick",
    "Oranthis",
    "Pinehaven",
    "Prairiewell",
    "Quartzbay",
    "Ravenwood",
    "Redwillow",
    "Ridgefern",
    "Riverlume",
    "Sablecrest",
    "Saffronridge",
    "Sageharbor",
    "Seabrook",
    "Silverfern",
    "Skymeadow",
    "Solvanta",
    "Southmere",
    "Starwell",
    "Stonebrook",
    "Sunharbor",
    "Terrafort",
    "Tervia",
    "Thornfield",
    "Timberlane",
    "Trunova",
    "Umberfield",
    "Valewood",
    "Vardent",
    "Verdantia",
    "Westbridge",
    "Willowmere",
    "Windcrest",
    "Winterbay",
    "Wrenstone",
    "Yuvantra",
    "Zenara",
    "Zephyrvale",
    "Auralis",
    "Brellon",
    "Corvanta",
    "Dovemere",
    "Elowen",
    "Farroway",
    "Greystone",
    "Harbora",
    "Iverna",
    "Jaspire",
    "Koralyn",
    "Luminor",
    "Montara",
    "Norvella",
    "Opalwick",
    "Paravue",
    "Quillstone",
    "Rosethorn",
    "Sylvara",
    "Torwyn",
    "Virelia",
    "Weylora",
    "Zinnwell",
)

SHOWCASE_SECTOR_DESCRIPTORS_V2: Final[tuple[str, ...]] = (
    "Agri Supply",
    "Mineral Resources",
    "Food Products",
    "Textile Mills",
    "Specialty Chemicals",
    "Life Sciences",
    "Metalworks",
    "Precision Engineering",
    "Mobility Components",
    "Power Systems",
    "Water Infrastructure",
    "Civil Projects",
    "Wholesale Markets",
    "Consumer Retail",
    "Road Logistics",
    "Marine Services",
    "Aviation Support",
    "Hospitality Services",
    "Digital Networks",
    "Software Systems",
    "Financial Services",
    "Urban Estates",
    "Advisory Services",
    "Equipment Leasing",
)

PORTFOLIO_DESCRIPTORS_V2: Final[tuple[str, ...]] = (
    "Commerce",
    "Industrial Works",
    "Enterprise Solutions",
    "Commercial Ventures",
    "Integrated Services",
    "Manufacturing Works",
    "Project Systems",
    "Supply Networks",
    "Resource Management",
    "Process Industries",
    "Distribution Services",
    "Infrastructure Works",
    "Technology Services",
    "Engineering Works",
    "Consumer Ventures",
    "Materials",
    "Logistics",
    "Energy Systems",
    "Urban Projects",
    "Capital Services",
    "Business Solutions",
    "Equipment Services",
    "Trade Link",
    "Process Systems",
    "Market Services",
    "Product Industries",
    "Core Engineering",
    "Network Services",
    "Development Company",
    "Operations",
    "Commercial Systems",
    "Industrial Services",
    "Applied Technologies",
    "Growth Ventures",
    "Regional Supply",
    "Technical Services",
    "Resource Ventures",
    "Integrated Projects",
    "Business Networks",
    "Enterprise Works",
    "Strategic Services",
    "Commercial Products",
)

GROUP_DESCRIPTORS_V2: Final[tuple[str, ...]] = (
    "Holdings Private Limited",
    "Ventures Private Limited",
    "Enterprises Private Limited",
    "Investments Private Limited",
    "Commercial Holdings Private Limited",
)

# Exact token matches are used, not substring matches (for example, ``tata``
# must not accidentally reject an unrelated longer invented word).
PROMINENT_BRAND_DENYLIST: Final[frozenset[str]] = frozenset(
    {
        "adani",
        "airtel",
        "bajaj",
        "birla",
        "godrej",
        "hdfc",
        "icici",
        "infosys",
        "kotak",
        "mahindra",
        "reliance",
        "tata",
        "vedanta",
        "wipro",
    }
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


def build_legacy_legal_name(random_source: Random, sequence: int) -> str:
    """Return the exact v1 name, retained only for safe upgrade matching."""

    if sequence < 1:
        raise ValueError("A legal-name sequence must be positive.")
    director = random_source.choice(GIVEN_NAMES)
    surname = random_source.choice(SURNAMES)
    descriptor = random_source.choice(BUSINESS_DESCRIPTORS)
    # The sequence is part of the name, rather than only the reference, so a
    # generated book never relies on accidental uniqueness in the corpus.
    return f"{surname} {descriptor} {director} {sequence:05d} Private Limited"


def build_legal_name(random_source: Random, sequence: int) -> str:
    """Return the stable, natural-looking v2 legal name for ``sequence``.

    Three legacy choices are deliberately consumed before returning the v2
    value.  The old name generator used those draws, and preserving the random
    stream means a naming-only upgrade cannot alter CINs, PANs, financials or
    facilities generated later in the reference book.
    """

    if sequence < 1:
        raise ValueError("A legal-name sequence must be positive.")
    random_source.choice(GIVEN_NAMES)
    random_source.choice(SURNAMES)
    random_source.choice(BUSINESS_DESCRIPTORS)
    return legal_name_v2(sequence)


def legal_name_v2(sequence: int) -> str:
    """Build a v2 legal name without depending on mutable random state."""

    if not 1 <= sequence <= 99_999:
        raise ValueError("A legal-name sequence must be between 1 and 99999.")
    root_count = len(FICTIONAL_BRAND_ROOTS_V2)
    root = FICTIONAL_BRAND_ROOTS_V2[(sequence - 1) % root_count]
    if sequence <= 120:
        descriptor = SHOWCASE_SECTOR_DESCRIPTORS_V2[(sequence - 1) % 24]
    else:
        descriptor_index = (sequence - 121) // root_count
        if descriptor_index >= len(PORTFOLIO_DESCRIPTORS_V2):
            raise ValueError("The v2 identity corpus is exhausted for this sequence.")
        descriptor = PORTFOLIO_DESCRIPTORS_V2[descriptor_index]
    return f"{root} {descriptor} Private Limited"


def build_group_name(sequence: int) -> str:
    """Return a stable fictional group legal name for one-based ``sequence``."""

    if not 1 <= sequence <= len(FICTIONAL_BRAND_ROOTS_V2) * len(GROUP_DESCRIPTORS_V2):
        raise ValueError("The v2 group-name corpus is exhausted for this sequence.")
    root_count = len(FICTIONAL_BRAND_ROOTS_V2)
    root = FICTIONAL_BRAND_ROOTS_V2[(sequence - 1) % root_count]
    descriptor = GROUP_DESCRIPTORS_V2[(sequence - 1) // root_count]
    return f"{root} {descriptor}"


def build_contact_email(sequence: int) -> str:
    """Return a non-deliverable address derived from the borrower's v2 name."""

    legal_name = legal_name_v2(sequence)
    domain_label = re.sub(r"[^a-z0-9]+", "-", legal_name.lower()).strip("-")
    suffix = "-private-limited"
    if domain_label.endswith(suffix):
        domain_label = domain_label[: -len(suffix)]
    return f"finance@{domain_label}.example"


def validate_v2_legal_names(names: tuple[str, ...]) -> None:
    """Reject collisions, real-brand tokens and visibly generated v1 shapes."""

    if not names:
        raise ValueError("The v2 legal-name collection must not be empty.")
    normalised = [re.sub(r"\s+", " ", value.strip()).casefold() for value in names]
    if len(normalised) != len(set(normalised)):
        raise ValueError("The v2 legal-name collection contains duplicate names.")
    legacy_pattern = re.compile(r"\b\d{5}\b")
    for value in names:
        if not value.endswith(" Private Limited"):
            raise ValueError(f"V2 legal name has an invalid constitution suffix: {value!r}.")
        if len(value) > 300 or legacy_pattern.search(value):
            raise ValueError(f"V2 legal name has an invalid display shape: {value!r}.")
        tokens = frozenset(re.findall(r"[a-z]+", value.casefold()))
        conflict = tokens & PROMINENT_BRAND_DENYLIST
        if conflict:
            raise ValueError(
                f"V2 legal name contains a denied public-brand token: {sorted(conflict)!r}."
            )


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
    "FICTIONAL_BRAND_ROOTS_V2",
    "GIVEN_NAMES",
    "GROUP_DESCRIPTORS_V2",
    "INDIAN_STATE_CODES",
    "NameFactory",
    "PORTFOLIO_DESCRIPTORS_V2",
    "PROMINENT_BRAND_DENYLIST",
    "SURNAMES",
    "SHOWCASE_SECTOR_DESCRIPTORS_V2",
    "build_contact_email",
    "build_contact_name",
    "build_group_name",
    "build_cin",
    "build_legacy_legal_name",
    "build_legal_name",
    "build_pan",
    "is_valid_cin",
    "is_valid_pan",
    "legal_name_v2",
    "validate_v2_legal_names",
]
