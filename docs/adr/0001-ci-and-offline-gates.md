# ADR-0001: CI gates run tests offline by default

## Status

Accepted

## Context

Covenant Radar must be deployable in air-gapped environments. A test that can silently use the
public network does not exercise that constraint and can produce results that cannot be reproduced
in a restricted deployment. CI also needs durable evidence for milestone decisions.

## Decision

The test suite installs a session-scoped socket guard that allows loopback and Unix-domain sockets
only. DNS resolution and socket connections to any other host raise an explicit test failure.

CI installs the project's locked dependencies on every job, starts PostgreSQL as a service for the
integration job, and checks that service before a suite can run. An unavailable database is a
failure with PostgreSQL named in the diagnostic. The pipeline retains coverage, evaluation,
browser, performance, accessibility, and security outputs as named artifacts when their producing
suites are available.

Release automation builds the distributions, produces a CycloneDX bill of materials, and attaches
the resulting evidence pack to published releases.

## Consequences

Tests that need an external service must use an explicitly provisioned local dependency, such as
the CI PostgreSQL service. Test helpers cannot perform implicit DNS lookups or outbound HTTP calls.
New evidence-producing suites must write to the artifact paths declared by the workflows so their
outputs remain available after the run.
