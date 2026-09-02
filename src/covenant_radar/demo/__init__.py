"""Deterministic, audit-preserving demo environment orchestration."""

from covenant_radar.demo.identities import IdentityUpgradeReport, upgrade_reference_identities
from covenant_radar.demo.manifest import SHOWCASE_BORROWER_COUNT, Scenario, scenario_manifest
from covenant_radar.demo.personas import DEMO_PASSWORD, PersonaReport, ensure_demo_personas
from covenant_radar.demo.showcase import ShowcaseInputReport, seed_showcase_inputs

__all__ = [
    "IdentityUpgradeReport",
    "DEMO_PASSWORD",
    "PersonaReport",
    "SHOWCASE_BORROWER_COUNT",
    "Scenario",
    "ShowcaseInputReport",
    "scenario_manifest",
    "ensure_demo_personas",
    "seed_showcase_inputs",
    "upgrade_reference_identities",
]
