"""Versioned reference-data loading for Covenant Radar.

The seed command is deliberately a small adapter around :mod:`loader`.  The
loader validates the complete input catalog before it mutates a session and
does not commit the caller's transaction.  This makes the same code safe for
the command line, integration tests, and a future administrative workflow.
"""

from covenant_radar.db.seed.loader import (
    DEFAULT_DATA_DIR,
    ReferenceDataError,
    ReferenceDataLoader,
    SeedLoader,
    SeedReport,
    deterministic_catalog_hash,
    load_reference_data,
)

__all__ = [
    "DEFAULT_DATA_DIR",
    "ReferenceDataError",
    "ReferenceDataLoader",
    "SeedLoader",
    "SeedReport",
    "deterministic_catalog_hash",
    "load_reference_data",
]
