"""Pure signal-domain contracts used by every ingestion source.

The domain deliberately knows nothing about SQLAlchemy, FastAPI, or a source
adapter.  A connector, file reader, feed, or API request is converted to the
same :class:`SignalEvent` before it reaches the application service.
"""

from __future__ import annotations

from covenant_radar.domain.signals.supersession import (
    CONTRADICTION_RULES,
    DEFAULT_CONTRADICTION_RULES,
    SUPERSESSION_RULES,
    ContradictionRule,
    SupersessionBatch,
    SupersessionResult,
    SupersessionRule,
    apply_supersession,
    contradiction_rules,
    find_rule,
    point_in_time,
    read_as_of,
    reconstruct_as_of,
    resolve_supersession,
    score_supersession,
    supersede,
)
from covenant_radar.domain.signals.taxonomy import (
    EVENT_TYPES,
    FAMILIES,
    FAMILY_EVENT_TYPES,
    FAMILY_UNITS,
    REQUIRED_PAYLOAD_FIELDS,
    SIGNAL_FAMILIES,
    SignalEvent,
    SignalFamily,
    SignalTaxonomyError,
    SignalTypeDefinition,
    canonical_json,
    compute_content_hash,
    definition_for,
    required_payload_fields,
    signal_content_hash,
    validate_event,
)

__all__ = [
    "EVENT_TYPES",
    "FAMILIES",
    "FAMILY_EVENT_TYPES",
    "FAMILY_UNITS",
    "REQUIRED_PAYLOAD_FIELDS",
    "SIGNAL_FAMILIES",
    "SignalEvent",
    "SignalFamily",
    "SignalTaxonomyError",
    "SignalTypeDefinition",
    "canonical_json",
    "CONTRADICTION_RULES",
    "compute_content_hash",
    "ContradictionRule",
    "DEFAULT_CONTRADICTION_RULES",
    "definition_for",
    "find_rule",
    "point_in_time",
    "required_payload_fields",
    "read_as_of",
    "reconstruct_as_of",
    "resolve_supersession",
    "signal_content_hash",
    "SUPERSESSION_RULES",
    "SupersessionBatch",
    "SupersessionRule",
    "SupersessionResult",
    "score_supersession",
    "supersede",
    "apply_supersession",
    "contradiction_rules",
    "validate_event",
]
