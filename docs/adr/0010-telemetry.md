# ADR-0010: Opt-out, content-free, documented telemetry

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

Usage data guides the roadmap and the open-core business, but trust matters more.

## Decision

Opseval sends anonymous, aggregated counts: a random install id, version, OS/arch, mode, and bucketed counts of runs, attempts, judged attempts, scenarios, users and projects, plus the transports and scorer ids in use. It **never** sends conversation text, scenarios, prompts, hosts, keys or personal data. Telemetry is disabled with `OPSEVAL_TELEMETRY=false` and off by default in CI (`CI=true`). `opseval telemetry show` prints the exact payload.

## Consequences

- Public metrics are reported as "among reporting installs".
- The payload schema is versioned and documented.
