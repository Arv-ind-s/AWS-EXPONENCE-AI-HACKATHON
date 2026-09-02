"""Accessibility gate for T-082: every token pair, both themes, both floors.

`scripts/check_contrast.py` already carries the WCAG maths and the token
contract independently of the application package (see `tests/unit/
test_tokens.py`, which proves the parser and the scan). This module is the
a11y-layer proof the task asks for by name — `test_every_pair_meets_floor_
in_both_themes` — read directly against the shipped `tokens.css`, plus the
`prefers-contrast: more` variant this task adds on top of it.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TOKEN_PATH = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "css" / "tokens.css"
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
pytestmark = pytest.mark.a11y


def test_every_pair_meets_floor_in_both_themes() -> None:
    """Both themes clear their WCAG floor on every role pair in use."""
    reports = checker.validate_token_css(TOKEN_PATH)

    assert reports, "the contrast check produced no evidence"
    pairs_seen = {theme: set() for theme in ("light", "dark")}
    for theme, pair, ratio in reports:
        pairs_seen[theme].add(pair.name)
        assert ratio >= pair.minimum, f"{theme} {pair.name}: {ratio:.3f} < {pair.minimum:.1f}"

    css = TOKEN_PATH.read_text(encoding="utf-8")
    for theme, selector in (("light", ":root"), ("dark", '[data-theme="dark"]')):
        expected = {pair.name for pair in checker.contrast_pairs(
            checker._parse_block_properties(css, selector)
        )}
        assert pairs_seen[theme] == expected


def test_accent_chips_meet_their_floor_in_dark_mode() -> None:
    """The dark palette's chip labels specifically clear the 4.5:1 floor.

    Named directly because a chip is the one place an accent colour sits on
    a *tinted* background rather than the page ground — the easiest place
    for a completed dark theme to quietly fall short.
    """
    css = TOKEN_PATH.read_text(encoding="utf-8")
    dark = checker._parse_block_properties(css, '[data-theme="dark"]')

    for role in ("--headroom-bg", "--watch-bg", "--breach-bg"):
        ratio = checker.contrast_ratio(dark["--ink"], dark[role])
        assert ratio >= 4.5, f"dark chip label on {role}: {ratio:.3f} < 4.5"


def test_a_token_missing_its_dark_value_fails_naming_it(tmp_path: Path) -> None:
    """The gate names the token, rather than failing silently or vaguely."""
    css = TOKEN_PATH.read_text(encoding="utf-8")
    pattern = r"(\[data-theme=\"dark\"\]\s*\{[^}]*?)\n\s*--breach:[^;]+;"
    broken = re.sub(pattern, r"\1", css, count=1)
    assert broken != css
    broken_path = tmp_path / "broken-tokens.css"
    broken_path.write_text(broken, encoding="utf-8")

    with pytest.raises(checker.TokenCheckError, match="--breach"):
        checker.validate_token_css(broken_path)


def test_high_contrast_variant_widens_the_margin_in_both_themes() -> None:
    """`prefers-contrast: more` moves ink-muted further from the floor.

    This is the "high-contrast variant" R-36 asks for: not a third palette
    to maintain, just a wider margin above the same floor for a reader who
    has told the OS they want one.
    """
    css = TOKEN_PATH.read_text(encoding="utf-8")
    media_match = re.search(
        r"@media\s*\(prefers-contrast:\s*more\)\s*\{(?P<body>.*)\}\s*$", css, flags=re.DOTALL
    )
    assert media_match is not None
    body = media_match.group("body")

    root_ratio_before = checker.contrast_ratio(
        checker._parse_block_properties(css, ":root")["--ink-muted"],
        checker._parse_block_properties(css, ":root")["--paper"],
    )
    dark_ratio_before = checker.contrast_ratio(
        checker._parse_block_properties(css, '[data-theme="dark"]')["--ink-muted"],
        checker._parse_block_properties(css, '[data-theme="dark"]')["--paper"],
    )

    light_override = checker._parse_block_properties(body, ":root")
    dark_override = checker._parse_block_properties(body, '[data-theme="dark"]')

    light_paper = checker._parse_block_properties(css, ":root")["--paper"]
    dark_paper = checker._parse_block_properties(css, '[data-theme="dark"]')["--paper"]

    root_ratio_after = checker.contrast_ratio(light_override["--ink-muted"], light_paper)
    dark_ratio_after = checker.contrast_ratio(dark_override["--ink-muted"], dark_paper)

    assert root_ratio_after > root_ratio_before >= 4.5
    assert dark_ratio_after > dark_ratio_before >= 4.5
