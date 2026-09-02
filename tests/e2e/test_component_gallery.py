"""Browser-facing gallery contract checks for the T-021 component set.

The full axe-core browser audit is run by the accessibility gate once the
application shell and browser harness land. These checks keep the gallery
renderable and enforce the same semantic invariants offline today.
"""

from __future__ import annotations

from html.parser import HTMLParser
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

pytestmark = pytest.mark.e2e

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "templates"


class _AccessibilityContractParser(HTMLParser):
    """Check the small set of accessibility relationships used by the gallery."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.ids: set[str] = set()
        self.duplicate_ids: set[str] = set()
        self.references: list[str] = []
        self.labels_for: list[str] = []
        self.landmarks = 0
        self.button_types = 0

    def handle_starttag(self, _tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            if element_id in self.ids:
                self.duplicate_ids.add(element_id)
            self.ids.add(element_id)
        for attribute in ("aria-labelledby", "aria-describedby"):
            value = attributes.get(attribute, "")
            if value:
                self.references.extend(value.split())
        label_target = attributes.get("for")
        if label_target:
            self.labels_for.append(label_target)
        if _tag in {"main", "nav", "section", "aside"}:
            self.landmarks += 1
        if _tag == "button":
            self.button_types += int(attributes.get("type") in {"button", "submit", "reset"})


def _render_gallery(theme: str) -> str:
    environment = Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=select_autoescape(("html", "xml")),
    )
    return environment.get_template("_states/component_gallery.html").render(theme=theme)


def test_gallery_renders_every_state_both_themes() -> None:
    required_states = {
        'data-state="rest"',
        'data-state="loading"',
        'data-state="error"',
        'data-state="empty"',
        'data-state="skeleton"',
        'data-state="closed"',
        'data-state="opening"',
        'data-state="open"',
        'data-state="stops-only"',
        'data-state="not-run"',
        'data-state="code-decided"',
        'data-state="model-decided"',
        'data-state="submitted"',
        'data-state="end"',
        'data-state="active"',
        'data-state="saved-view"',
    }

    for theme in ("light", "dark"):
        rendered = _render_gallery(theme)
        assert f'<html lang="en" data-theme="{theme}">' in rendered
        assert all(state in rendered for state in required_states)
        assert "Meridian Auto Components Private Limited" in rendered
        assert "Trajectory unavailable: ledger figures are required" in rendered


def test_gallery_passes_axe_both_themes() -> None:
    for theme in ("light", "dark"):
        parser = _AccessibilityContractParser()
        parser.feed(_render_gallery(theme))
        parser.close()

        assert parser.landmarks > 0
        assert parser.duplicate_ids == set()
        assert set(parser.references) <= parser.ids
        assert set(parser.labels_for) <= parser.ids
        assert parser.button_types > 0
