# ADR-0005: One storage contract for local and server modes

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

Opseval must run on a laptop or in CI with no database (local mode), and also as a shared, horizontally scalable service (server mode), without a rewrite.

## Decision

- A `Storage` protocol with two implementations: **FileStore** (`reports/` on disk) and **SqlStore** (PostgreSQL for metadata and results, plus S3-compatible object storage for immutable run records and reports).
- Both use the same object layout: `<prefix>/<project>/<run_id>/{manifest.json, records/<test>__attempt<N>.json, result.json, report.html}`.
- A queue on Redis/Valkey plus worker processes replaces any in-process run state in server mode.

## Consequences

- Offline `rescore` and migration between modes need only the layout contract.
- ClickHouse is not needed at evaluation data volumes. We will revisit if Cloud analytics require it.
