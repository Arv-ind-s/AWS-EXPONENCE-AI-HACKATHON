"""Validate Covenant Radar's token contract without third-party dependencies.

The check is intentionally independent of the application package. It can run
from a source checkout before installation and is suitable for the offline
quality gate. It validates the token vocabulary, WCAG contrast ratios, local
design-literal policy, font licensing, and the glyphs needed by the locale
stacks.
"""

from __future__ import annotations

import re
import struct
import sys
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

PROJECT_ROOT: Final[Path] = Path(__file__).resolve().parents[1]
TOKEN_PATH: Final[Path] = (
    PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "css" / "tokens.css"
)
WEB_ROOT: Final[Path] = PROJECT_ROOT / "src" / "covenant_radar" / "web"
FONT_ROOT: Final[Path] = WEB_ROOT / "static" / "fonts"
LICENSE_PATH: Final[Path] = FONT_ROOT / "LICENSES.md"

EXPECTED_TOKENS: Final[tuple[str, ...]] = (
    "--paper",
    "--ink",
    "--ink-muted",
    "--hairline",
    "--surface-raised",
    "--surface-subtle",
    "--surface-sunken",
    "--surface-overlay",
    "--focus",
    "--accent",
    "--accent-fill",
    "--accent-strong",
    "--accent-soft",
    "--accent-contrast",
    "--signal",
    "--signal-soft",
    "--headroom",
    "--watch",
    "--breach",
    "--headroom-bg",
    "--watch-bg",
    "--breach-bg",
    "--font-head",
    "--font-editorial",
    "--font-data",
    "--font-ui",
    "--font-deva",
    "--font-heading",
    "--size-caption",
    "--size-data",
    "--size-body",
    "--size-section",
    "--size-section-title",
    "--size-case-title",
    "--size-screen-title",
    "--size-display",
    "--weight-regular",
    "--weight-medium",
    "--weight-semi",
    "--weight-bold",
    "--space-1",
    "--space-2",
    "--space-3",
    "--space-4",
    "--space-5",
    "--space-6",
    "--space-8",
    "--space-10",
    "--space-12",
    "--space-16",
    "--row-height",
    "--control-height",
    "--state-min-height",
    "--content-max",
    "--sidebar-width",
    "--sidebar-collapsed",
    "--topbar-height",
    "--grid-cols",
    "--hit-min",
    "--radius",
    "--radius-sm",
    "--radius-md",
    "--radius-lg",
    "--radius-pill",
    "--border",
    "--border-strong",
    "--border-width",
    "--tracking-label",
    "--shadow-sm",
    "--shadow-md",
    "--shadow-lg",
    "--shadow-drawer",
    "--shadow-1",
    "--shadow-2",
    "--dur-state",
    "--dur-panel",
    "--dur-page",
    "--dur-fast",
    "--dur-medium",
    "--dur-slow",
    "--ease",
    "--ease-spring",
)

COLOUR_TOKENS: Final[tuple[str, ...]] = (
    "--paper",
    "--ink",
    "--ink-muted",
    "--hairline",
    "--surface-raised",
    "--surface-subtle",
    "--surface-sunken",
    "--surface-overlay",
    "--focus",
    "--accent",
    "--accent-fill",
    "--accent-strong",
    "--accent-soft",
    "--accent-contrast",
    "--signal",
    "--signal-soft",
    "--headroom",
    "--watch",
    "--breach",
    "--headroom-bg",
    "--watch-bg",
    "--breach-bg",
)

REQUIRED_GLYPHS: Final[tuple[str, ...]] = ("₹", "अ")
ALLOWED_SELECTORS: Final[frozenset[str]] = frozenset({":root", '[data-theme="dark"]'})
FONT_GLYPH_REQUIREMENTS: Final[dict[str, tuple[str, ...]]] = {
    "radar-serif.ttf": ("₹",),
    "radar-mono-regular.ttf": ("₹",),
    "radar-mono-semibold.ttf": ("₹",),
    "radar-mono-bold.ttf": ("₹",),
    "radar-sans.ttf": ("₹",),
    "radar-devanagari.ttf": REQUIRED_GLYPHS,
}


class TokenCheckError(ValueError):
    """Raised when a design-system invariant is not satisfied."""


@dataclass(frozen=True, slots=True)
class ContrastPair:
    """One foreground/background role pair and its WCAG floor."""

    name: str
    foreground: str
    background: str
    minimum: float


@dataclass(frozen=True, slots=True)
class DesignLiteral:
    """A forbidden literal and its source location."""

    path: Path
    line: int
    literal: str

    def describe(self) -> str:
        return f"{self.path}:{self.line}: design literal {self.literal!r}"


@dataclass(frozen=True, slots=True)
class FontCoverage:
    """Coverage result for one required glyph in one font file."""

    path: Path
    glyph: str
    covered: bool


_PROPERTY_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?P<name>--[a-z0-9-]+)\s*:\s*(?P<value>[^;{}]+)\s*;", re.IGNORECASE
)
_HEX_LITERAL_PATTERN: Final[re.Pattern[str]] = re.compile(r"#[0-9a-f]{3,8}\b", re.IGNORECASE)
_DIMENSION_LITERAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"(?<![a-z0-9_-])\d+(?:\.\d+)?(?:px|ms)\b", re.IGNORECASE
)
_EASING_LITERAL_PATTERN: Final[re.Pattern[str]] = re.compile(
    r"\b(?:cubic-bezier|steps)\s*\([^)]*\)", re.IGNORECASE
)
_DESIGN_FILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".css", ".html", ".htm", ".jinja", ".jinja2"}
)
_FONT_SUFFIXES: Final[frozenset[str]] = frozenset({".otf", ".ttf"})


def _without_comments(css: str) -> str:
    return re.sub(r"/\*.*?\*/", "", css, flags=re.DOTALL)


def parse_custom_properties(css: str) -> dict[str, str]:
    """Return custom-property declarations in source order-independent form."""
    return {
        match.group("name"): match.group("value").strip()
        for match in _PROPERTY_PATTERN.finditer(_without_comments(css))
    }


def _parse_block_properties(css: str, selector: str) -> dict[str, str]:
    pattern = re.compile(rf"{re.escape(selector)}\s*\{{(?P<body>[^{{}}]*)\}}", re.DOTALL)
    match = pattern.search(_without_comments(css))
    if match is None:
        raise TokenCheckError(f"tokens.css is missing the {selector} block")
    return parse_custom_properties(match.group("body"))


def _extract_top_level_selectors(css: str) -> tuple[str, ...]:
    """Extract selectors while ignoring declarations and permitted at-rules."""
    source = _without_comments(css)
    selectors: list[str] = []
    depth = 0
    block_start = 0
    index = 0
    while index < len(source):
        character = source[index]
        if character == "{":
            prelude = source[block_start:index].strip()
            if prelude and not prelude.startswith("@"):
                selectors.append(prelude)
            depth += 1
            block_start = index + 1
        elif character == "}":
            depth = max(depth - 1, 0)
            block_start = index + 1
        index += 1
    return tuple(selectors)


def _theme_values(css: str, selector: str) -> dict[str, str]:
    values = _parse_block_properties(css, selector)
    missing = [name for name in COLOUR_TOKENS if name not in values]
    if selector == ":root":
        missing = [name for name in EXPECTED_TOKENS if name not in values]
    if missing:
        raise TokenCheckError(f"{selector} is missing token(s): {', '.join(missing)}")
    return values


def _hex_colour(value: str, token: str) -> tuple[int, int, int]:
    match = re.fullmatch(r"#(?P<hex>[0-9a-f]{6})", value.strip(), re.IGNORECASE)
    if match is None:
        raise TokenCheckError(f"{token} must be a six-digit hexadecimal colour, got {value!r}")
    channels = match.group("hex")
    return (
        int(channels[0:2], 16),
        int(channels[2:4], 16),
        int(channels[4:6], 16),
    )


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    channels = [channel / 255 for channel in rgb]
    linear = [
        channel / 12.92 if channel <= 0.04045 else ((channel + 0.055) / 1.055) ** 2.4
        for channel in channels
    ]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast_ratio(foreground: str, background: str) -> float:
    """Return the WCAG 2.x contrast ratio for two six-digit hex colours."""
    foreground_luminance = _relative_luminance(_hex_colour(foreground, "foreground"))
    background_luminance = _relative_luminance(_hex_colour(background, "background"))
    lighter = max(foreground_luminance, background_luminance)
    darker = min(foreground_luminance, background_luminance)
    return (lighter + 0.05) / (darker + 0.05)


def contrast_pairs(theme: dict[str, str]) -> tuple[ContrastPair, ...]:
    """Build the role pairs actually used by the two themes.

    Accent roles are checked on the document ground because the accent is used
    as ink in the ledger and forecast mark. Tinted chip surfaces are checked
    with the ink role used for their labels, so a chip cannot pass by hiding
    low-contrast text in a risk-colour background.
    """
    return (
        ContrastPair("primary text on paper", theme["--ink"], theme["--paper"], 7.0),
        ContrastPair(
            "primary text on raised surface", theme["--ink"], theme["--surface-raised"], 7.0
        ),
        ContrastPair(
            "primary text on subtle surface", theme["--ink"], theme["--surface-subtle"], 7.0
        ),
        ContrastPair("secondary text on paper", theme["--ink-muted"], theme["--paper"], 4.5),
        ContrastPair("accent on paper", theme["--accent"], theme["--paper"], 4.5),
        ContrastPair("signal on paper", theme["--signal"], theme["--paper"], 4.5),
        ContrastPair(
            "accent button label", theme["--accent-contrast"], theme["--accent-fill"], 4.5
        ),
        ContrastPair("headroom accent on paper", theme["--headroom"], theme["--paper"], 4.5),
        ContrastPair("watch accent on paper", theme["--watch"], theme["--paper"], 4.5),
        ContrastPair("breach accent on paper", theme["--breach"], theme["--paper"], 4.5),
        ContrastPair("headroom chip label", theme["--ink"], theme["--headroom-bg"], 4.5),
        ContrastPair("watch chip label", theme["--ink"], theme["--watch-bg"], 4.5),
        ContrastPair("breach chip label", theme["--ink"], theme["--breach-bg"], 4.5),
    )


def check_contrast(css: str) -> tuple[tuple[str, ContrastPair, float], ...]:
    """Validate both themes and return their measured ratios."""
    root = _theme_values(css, ":root")
    dark = _theme_values(css, '[data-theme="dark"]')
    reports: list[tuple[str, ContrastPair, float]] = []
    for theme_name, values in (("light", root), ("dark", dark)):
        for pair in contrast_pairs(values):
            ratio = contrast_ratio(pair.foreground, pair.background)
            if ratio < pair.minimum:
                raise TokenCheckError(
                    f"{theme_name} {pair.name}: ratio {ratio:.3f} is below required "
                    f"{pair.minimum:.1f}"
                )
            reports.append((theme_name, pair, ratio))
    return tuple(reports)


def validate_token_css(path: Path = TOKEN_PATH) -> tuple[tuple[str, ContrastPair, float], ...]:
    """Validate the complete token stylesheet and return contrast evidence."""
    if not path.is_file():
        raise TokenCheckError(f"token stylesheet does not exist: {path}")
    css = path.read_text(encoding="utf-8")
    properties = parse_custom_properties(css)
    declared = tuple(properties)
    missing = [token for token in EXPECTED_TOKENS if token not in properties]
    extra = [token for token in declared if token not in EXPECTED_TOKENS]
    if missing or extra:
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise TokenCheckError("token vocabulary mismatch: " + "; ".join(details))

    selectors = _extract_top_level_selectors(css)
    unknown = [selector for selector in selectors if selector not in ALLOWED_SELECTORS]
    if unknown:
        raise TokenCheckError(
            "unexpected selector(s): " + ", ".join(repr(item) for item in unknown)
        )

    return check_contrast(css)


def scan_design_literals(
    root: Path | str, *, token_path: Path = TOKEN_PATH
) -> tuple[DesignLiteral, ...]:
    """Find design literals in stylesheets and templates outside token CSS."""
    source = Path(root)
    paths: Iterable[Path] = (source,) if source.is_file() else source.rglob("*")
    findings: list[DesignLiteral] = []
    resolved_token_path = token_path.resolve()
    for path in sorted(paths):
        if not path.is_file() or path.suffix.lower() not in _DESIGN_FILE_SUFFIXES:
            continue
        if path.resolve() == resolved_token_path:
            continue
        content = path.read_text(encoding="utf-8")
        for line_number, line in enumerate(content.splitlines(), start=1):
            matches = (
                _HEX_LITERAL_PATTERN.finditer(line),
                _DIMENSION_LITERAL_PATTERN.finditer(line),
                _EASING_LITERAL_PATTERN.finditer(line),
            )
            for line_matches in matches:
                findings.extend(
                    DesignLiteral(path=path, line=line_number, literal=match.group(0))
                    for match in line_matches
                )
    return tuple(findings)


def _read_sfnt_tables(data: bytes) -> dict[bytes, tuple[int, int]]:
    if len(data) < 12 or data[:4] not in {b"\x00\x01\x00\x00", b"OTTO", b"true", b"typ1"}:
        raise TokenCheckError("font is not a supported OpenType/TrueType file")
    table_count = struct.unpack_from(">H", data, 4)[0]
    tables: dict[bytes, tuple[int, int]] = {}
    for index in range(table_count):
        offset = 12 + index * 16
        if offset + 16 > len(data):
            raise TokenCheckError("font table directory is truncated")
        tag, _checksum, table_offset, table_length = struct.unpack_from(">4sIII", data, offset)
        if table_offset + table_length > len(data):
            raise TokenCheckError(f"font table {tag.decode(errors='replace')!r} is truncated")
        tables[tag] = (table_offset, table_length)
    return tables


def _cmap_covers(data: bytes, cmap_offset: int, cmap_length: int, codepoint: int) -> bool:
    if cmap_length < 4:
        return False
    _version, subtable_count = struct.unpack_from(">HH", data, cmap_offset)
    for index in range(subtable_count):
        record_offset = cmap_offset + 4 + index * 8
        if record_offset + 8 > cmap_offset + cmap_length:
            return False
        _platform, _encoding, relative_offset = struct.unpack_from(">HHI", data, record_offset)
        subtable = cmap_offset + relative_offset
        if subtable + 2 > cmap_offset + cmap_length:
            continue
        format_number = struct.unpack_from(">H", data, subtable)[0]
        if format_number == 4 and subtable + 16 <= len(data):
            length, _language, segments_x2 = struct.unpack_from(">HHH", data, subtable + 2)
            segment_count = segments_x2 // 2
            if subtable + length > cmap_offset + cmap_length:
                continue
            end_codes = subtable + 14
            start_codes = end_codes + segment_count * 2 + 2
            id_deltas = start_codes + segment_count * 2
            id_ranges = id_deltas + segment_count * 2
            for segment in range(segment_count):
                end_code = struct.unpack_from(">H", data, end_codes + segment * 2)[0]
                start_code = struct.unpack_from(">H", data, start_codes + segment * 2)[0]
                if start_code > codepoint or end_code < codepoint:
                    continue
                if start_code == end_code == 0xFFFF:
                    continue
                delta = struct.unpack_from(">h", data, id_deltas + segment * 2)[0]
                range_offset = struct.unpack_from(">H", data, id_ranges + segment * 2)[0]
                if range_offset == 0:
                    return ((codepoint + delta) & 0xFFFF) != 0
                glyph_address = (
                    id_ranges + segment * 2 + range_offset + (codepoint - start_code) * 2
                )
                if glyph_address + 2 > subtable + length:
                    continue
                glyph = struct.unpack_from(">H", data, glyph_address)[0]
                return glyph != 0 and ((glyph + delta) & 0xFFFF) != 0
        elif format_number in {12, 13} and subtable + 16 <= len(data):
            _reserved, _length, _language, group_count = struct.unpack_from(
                ">HIII", data, subtable + 2
            )
            if _length < 16 or subtable + _length > cmap_offset + cmap_length:
                continue
            groups = subtable + 16
            if groups + group_count * 12 > subtable + _length:
                continue
            for group in range(group_count):
                group_offset = groups + group * 12
                if group_offset + 12 > len(data):
                    break
                start, end, glyph = struct.unpack_from(">III", data, group_offset)
                if start <= codepoint <= end:
                    return glyph != 0
    return False


def font_contains_codepoint(path: Path, codepoint: int) -> bool:
    """Read a cmap table and report whether a font supplies a glyph."""
    if not 0 <= codepoint <= 0x10FFFF:
        raise ValueError("codepoint must be between U+0000 and U+10FFFF")
    data = path.read_bytes()
    tables = _read_sfnt_tables(data)
    cmap = tables.get(b"cmap")
    if cmap is None:
        return False
    return _cmap_covers(data, cmap[0], cmap[1], codepoint)


def check_font_coverage(font_root: Path = FONT_ROOT) -> tuple[FontCoverage, ...]:
    """Require every local stack to cover its required glyphs.

    The data, heading, and UI stacks require the rupee sign. Hindi content is
    explicitly assigned the dedicated Devanagari stack, whose local face must
    supply both required glyphs; platform fallbacks remain valid for characters
    outside those declared responsibilities.
    """
    fonts = tuple(
        sorted(path for path in font_root.iterdir() if path.suffix.lower() in _FONT_SUFFIXES)
    )
    if not fonts:
        raise TokenCheckError(f"no vendored fonts found in {font_root}")
    missing_requirements = sorted(set(FONT_GLYPH_REQUIREMENTS) - {path.name for path in fonts})
    if missing_requirements:
        raise TokenCheckError(
            "font stack is missing registered file(s): " + ", ".join(missing_requirements)
        )
    coverage = tuple(
        FontCoverage(path, glyph, font_contains_codepoint(path, ord(glyph)))
        for path in fonts
        for glyph in FONT_GLYPH_REQUIREMENTS[path.name]
    )
    missing = [item for item in coverage if not item.covered]
    if missing:
        details = ", ".join(f"{item.path.name} lacks {item.glyph!r}" for item in missing)
        raise TokenCheckError("required glyph coverage is incomplete: " + details)
    return coverage


def missing_font_license_entries(
    font_root: Path = FONT_ROOT, license_path: Path = LICENSE_PATH
) -> tuple[Path, ...]:
    """Return vendored font files that are not named in the license register."""
    if not license_path.is_file():
        raise TokenCheckError(f"font license register does not exist: {license_path}")
    license_text = license_path.read_text(encoding="utf-8")
    fonts = tuple(
        sorted(path for path in font_root.iterdir() if path.suffix.lower() in _FONT_SUFFIXES)
    )
    return tuple(path for path in fonts if path.name not in license_text)


def run_checks() -> int:
    """Run all static design-system checks and print reviewable evidence."""
    reports = validate_token_css()
    literals = scan_design_literals(WEB_ROOT)
    if literals:
        raise TokenCheckError(
            "forbidden design literal(s): " + "; ".join(item.describe() for item in literals)
        )
    missing_licenses = missing_font_license_entries()
    if missing_licenses:
        names = ", ".join(path.name for path in missing_licenses)
        raise TokenCheckError("font license entries missing for: " + names)
    check_font_coverage()

    for theme, pair, ratio in reports:
        _write_line(
            f"PASS contrast {theme} / {pair.name}: {ratio:.3f} (minimum {pair.minimum:.1f})"
        )
    _write_line(f"PASS design literals: none outside {TOKEN_PATH.relative_to(PROJECT_ROOT)}")
    _write_line(f"PASS font licenses: {len(FONT_GLYPH_REQUIREMENTS)} font files registered")
    glyphs = ", ".join(f"U+{ord(glyph):04X}" for glyph in REQUIRED_GLYPHS)
    _write_line(f"PASS glyph coverage: {glyphs}")
    return 0


def _write_line(message: str, *, error: bool = False) -> None:
    """Write one CLI line without using a banned direct print call."""
    stream = sys.stderr if error else sys.stdout
    encoding = getattr(stream, "encoding", None) or "utf-8"
    safe_message = message.encode(encoding, errors="backslashreplace").decode(
        encoding, errors="replace"
    )
    stream.write(safe_message + "\n")


def main() -> int:
    try:
        return run_checks()
    except (OSError, UnicodeError, struct.error, TokenCheckError) as error:
        _write_line(f"FAIL {error}", error=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
