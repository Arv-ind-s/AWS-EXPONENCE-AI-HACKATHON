"""Unit tests for the design tokens, typography, and static visual policy."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "css" / "tokens.css"
FONT_ROOT = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "fonts"
CHECKER_PATH = PROJECT_ROOT / "scripts" / "check_contrast.py"


def _load_checker():
    spec = importlib.util.spec_from_file_location("covenant_radar_token_checker", CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()
pytestmark = pytest.mark.unit


def test_every_token_from_plan_present() -> None:
    css = TOKEN_PATH.read_text(encoding="utf-8")
    properties = checker.parse_custom_properties(css)

    assert tuple(properties) == checker.EXPECTED_TOKENS


def test_dark_theme_redefines_every_colour_role() -> None:
    css = TOKEN_PATH.read_text(encoding="utf-8")
    dark = checker._parse_block_properties(css, '[data-theme="dark"]')

    assert set(checker.COLOUR_TOKENS) <= set(dark)


def test_no_selector_beyond_root_theme_and_reduced_motion(tmp_path: Path) -> None:
    css = TOKEN_PATH.read_text(encoding="utf-8")

    assert set(checker._extract_top_level_selectors(css)) <= checker.ALLOWED_SELECTORS

    invalid = tmp_path / "invalid-tokens.css"
    invalid.write_text(
        css.replace("    --dur-state: 0ms;", "    body { color: red; }\n    --dur-state: 0ms;"),
        encoding="utf-8",
    )
    with pytest.raises(checker.TokenCheckError, match="unexpected selector"):
        checker.validate_token_css(invalid)


def test_reduced_motion_zeroes_durations() -> None:
    css = TOKEN_PATH.read_text(encoding="utf-8")
    media_match = re.search(
        r"@media\s*\(prefers-reduced-motion:\s*reduce\)\s*\{(?P<body>.*?)\}\s*$",
        css,
        flags=re.DOTALL,
    )
    assert media_match is not None

    reduced = checker.parse_custom_properties(media_match.group("body"))
    assert reduced["--dur-state"] == "0ms"
    assert reduced["--dur-panel"] == "0ms"


def test_no_design_literal_outside_tokens(tmp_path: Path) -> None:
    assert checker.validate_token_css(TOKEN_PATH)
    findings = checker.scan_design_literals(PROJECT_ROOT / "src" / "covenant_radar" / "web")

    assert findings == ()

    offending = tmp_path / "offending.css"
    offending.write_text(".bad { color: #FFFFFF; transition: opacity 160ms; }\n", encoding="utf-8")
    violation = checker.scan_design_literals(tmp_path)
    assert len(violation) == 2
    assert all(item.path == offending and item.line == 1 for item in violation)
    assert all("design literal" in item.describe() for item in violation)


def test_rupee_and_devanagari_covered() -> None:
    coverage = checker.check_font_coverage(FONT_ROOT)

    assert coverage
    assert all(item.covered for item in coverage)
    assert {item.glyph for item in coverage} == set(checker.REQUIRED_GLYPHS)


def test_every_font_has_a_licence_entry() -> None:
    assert checker.missing_font_license_entries(FONT_ROOT) == ()
