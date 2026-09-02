"""Unit coverage for T-021's token-only, accessible component contract."""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

PROJECT_ROOT = Path(__file__).resolve().parents[2]
TEMPLATE_ROOT = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "templates"
COMPONENT_ROOT = TEMPLATE_ROOT / "_components"
CSS_PATH = PROJECT_ROOT / "src" / "covenant_radar" / "web" / "static" / "css" / "app.css"
CHECKER_PATH = PROJECT_ROOT / "scripts" / "check_contrast.py"

pytestmark = pytest.mark.unit


def _environment() -> Environment:
    return Environment(
        loader=FileSystemLoader(str(TEMPLATE_ROOT)),
        autoescape=select_autoescape(("html", "xml")),
    )


def _render(template_name: str, macro_call: str, **context: object) -> str:
    source = f'{{% from "_components/{template_name}.html" import {template_name} %}}'
    source += f"{{{{ {macro_call} }}}}"
    template = _environment().from_string(source)
    return template.render(**context)


def _load_literal_checker():
    module_name = "covenant_radar_component_token_checker"
    spec = importlib.util.spec_from_file_location(module_name, CHECKER_PATH)
    if spec is None or spec.loader is None:
        raise AssertionError(f"Could not load {CHECKER_PATH}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def test_eighteen_macros_defined() -> None:
    expected = {
        "button",
        "field",
        "panel",
        "ledger_table",
        "band_chip",
        "verdict_mark",
        "drawer",
        "toast",
        "empty_state",
        "skeleton",
        "degraded_note",
        "trajectory",
        "horizon_control",
        "why_section",
        "feedback_control",
        "provenance_link",
        "pagination",
        "filter_bar",
    }
    discovered = {
        match.group(1)
        for path in COMPONENT_ROOT.glob("*.html")
        for match in re.finditer(r"\{%-?\s*macro\s+(\w+)", path.read_text(encoding="utf-8"))
    }

    assert expected <= discovered
    assert (COMPONENT_ROOT / "ledger_row.html").is_file()


def test_no_design_literal_in_app_css() -> None:
    checker = _load_literal_checker()

    assert checker.scan_design_literals(CSS_PATH) == ()


def test_band_chip_rejects_unknown_band() -> None:
    rendered = _render("band_chip", 'band_chip("critical", "Unclassified")')

    assert "band-chip--neutral" in rendered
    assert "band-chip--critical" not in rendered
    assert 'data-band="neutral"' in rendered


def test_empty_table_renders_empty_state() -> None:
    rendered = _render(
        "ledger_table",
        'ledger_table("Covenants", (("name", "Covenant"),), ())',
    )

    assert "state--empty" in rendered
    assert "No records" in rendered
    assert "<tbody" not in rendered


def test_trajectory_requires_ledger_figures() -> None:
    rendered = _render(
        "trajectory",
        'trajectory("forecast", "0,30 100,10", ())',
    )

    assert "ledger figures are required" in rendered
    assert "<svg" not in rendered


def test_long_name_wraps_not_truncates() -> None:
    name = "Meridian Auto Components Private Limited"
    rendered = _render(
        "ledger_table",
        'ledger_table("Borrowers", (("name", "Borrower"),), ({"name": name},))',
        name=name,
    )
    css = CSS_PATH.read_text(encoding="utf-8")

    assert name in rendered
    assert "…" not in rendered
    assert "overflow-wrap: anywhere" in css
    assert "text-overflow" not in css


def test_every_interactive_target_at_least_32px() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")

    assert css.count("min-height: var(--hit-min)") >= 3
    assert "min-width: var(--hit-min)" in css
    assert "32px" not in css


def test_only_drawer_casts_a_shadow() -> None:
    css = CSS_PATH.read_text(encoding="utf-8")
    declarations = re.findall(r"box-shadow\s*:", css)

    assert len(declarations) == 1
    assert '.drawer[data-state="open"]' in css
    assert "var(--shadow-drawer)" in css
