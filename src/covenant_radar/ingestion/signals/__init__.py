"""Signal-ingestion adapter contracts and validation pipeline."""

from __future__ import annotations

from covenant_radar.ingestion.signals.framework import (
    InMemorySignalQuarantine,
    PreparedSignal,
    QuarantinedSignal,
    SignalBatch,
    SignalIngestionFramework,
    SignalIngestionReport,
    SignalQuarantineSink,
)

__all__ = [
    "InMemorySignalQuarantine",
    "PreparedSignal",
    "QuarantinedSignal",
    "SignalBatch",
    "SignalIngestionFramework",
    "SignalIngestionReport",
    "SignalQuarantineSink",
]
