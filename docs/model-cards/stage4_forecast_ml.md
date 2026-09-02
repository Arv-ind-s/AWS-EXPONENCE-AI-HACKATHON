# Model card — `stage4_forecast_ml`

## Purpose

Produces calibrated 30/60/90-day covenant-breach probabilities from the
allow-listed, point-in-time structured Stage-4 feature snapshot. It never
calculates covenant compliance, a crossing date, a simulation, or a credit
decision.

## Controls

The local artifact is checksum-verified and registered with a named owner and
approval. The deterministic Stage-4 probability is retained as a fallback for
missing features, unavailable artifacts, or a governance rollback. Every
prediction records artifact version, feature snapshot hash and contributions
in the Stage-4 trace; identifiers, text and post-score outcomes are excluded.
