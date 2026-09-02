"""Static proof that outbound model calls remain behind one boundary."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.security

_SRC = Path(__file__).resolve().parents[2] / "src"
_ALLOWED_IMPORTERS = frozenset(
    {
        "covenant_radar.ai.intake",
        "covenant_radar.ai.memo",
        "covenant_radar.services.memo",
        # The browser composition root builds the one `ModelClient` and injects
        # it into the stage-1 proposal generator.  Constructing the client is
        # the composition root's job by design; what this test protects is that
        # nobody *calls* the model outside the client boundary, which the
        # `outbound_calls` assertion below enforces independently.
        "covenant_radar.web.application",
    }
)


def _module_name(path: Path) -> str:
    relative = path.relative_to(_SRC).with_suffix("")
    return ".".join(relative.parts)


def test_only_permitted_modules_import_the_client() -> None:
    importers: set[str] = set()
    outbound_calls: set[str] = set()
    for path in sorted(_SRC.rglob("*.py")):
        module = _module_name(path)
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module == "covenant_radar.ai.client":
                if module != "covenant_radar.ai.client":
                    importers.add(module)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "complete" and module not in {
                    "covenant_radar.ai.client",
                    "covenant_radar.ai.providers.base",
                    "covenant_radar.ai.providers.anthropic",
                    "covenant_radar.ai.providers.azure_openai",
                    "covenant_radar.ai.providers.recorded",
                    "covenant_radar.ai.providers.tcs_genailab",
                }:
                    outbound_calls.add(module)

    assert importers <= _ALLOWED_IMPORTERS
    assert outbound_calls == set()
