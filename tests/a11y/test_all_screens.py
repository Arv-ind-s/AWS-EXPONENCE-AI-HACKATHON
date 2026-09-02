"""T-083: the automated audit over every screen, in every reachable state,
in both themes.

`_screens.py` carries the manifest and the render helpers (each reusing an
existing feature's own integration-test fixture, per that module's
docstring); this module only asserts. `test_every_screen_and_state_covered`
is the coverage gate itself — `T-083`'s "a screen reachable only in a
state the audit does not cover -> the coverage test fails" requirement —
so it fails the moment a new template lands under `web/templates/screens/`
without a matching `ScreenCase`, not just when an existing one regresses.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.a11y._contract import assert_accessible
from tests.a11y._screens import COVERED_TEMPLATES, SCREENS

pytestmark = pytest.mark.a11y

_TEMPLATE_ROOT = (
    Path(__file__).resolve().parents[2] / "src" / "covenant_radar" / "web" / "templates"
)
_THEMES = ("light", "dark")


def _relative_screen_templates() -> frozenset[str]:
    return frozenset(
        path.relative_to(_TEMPLATE_ROOT).as_posix()
        for path in (_TEMPLATE_ROOT / "screens").rglob("*.html")
    )


def test_every_screen_and_state_covered() -> None:
    on_disk = _relative_screen_templates()
    missing = on_disk - COVERED_TEMPLATES
    assert not missing, (
        f"{len(missing)} screen template(s) exist with no ScreenCase in "
        f"tests/a11y/_screens.py: {sorted(missing)}"
    )
    stale = COVERED_TEMPLATES - on_disk
    assert not stale, (
        f"tests/a11y/_screens.py names template(s) that no longer exist: {sorted(stale)}"
    )
    for case in SCREENS:
        assert case.states, f"{case.name}: a ScreenCase must cover at least one state"


@pytest.mark.parametrize("case", SCREENS, ids=lambda case: case.name)
def test_zero_violations_both_themes(case: object) -> None:
    """Every state, in both themes, clears the contract with zero
    violations. Theme *fidelity* (does the page actually honour the
    `covenant_radar_theme` cookie) is `T-082`'s and `test_why_panel_a11y`'s
    concern; this test's job is that whatever the route renders is
    accessible, which is why it runs each state under both cookie values
    rather than asserting on the resulting markup's theme attribute."""
    for state in case.states:  # type: ignore[attr-defined]
        for theme in _THEMES:
            html = state.render(theme)  # type: ignore[attr-defined]
            assert_accessible(
                html,
                screen=f"{case.name}/{state.name}/{theme}",  # type: ignore[attr-defined]
                fragment=case.fragment,  # type: ignore[attr-defined]
            )
