# ADR-0001: Record architecture decisions

- **Status:** Accepted
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

Opseval is built in the open, and contributors must be able to see why the project made each decision.

## Decision

We record significant architecture and policy decisions as ADRs in `docs/adr/`, using the template in `template.md`. Changes to public contracts go through an RFC in GitHub Discussions first; the outcome is written up as an ADR.

## Consequences

- Decisions are discoverable and reviewable.
- A superseded ADR is marked `Superseded by ADR-NNNN`, never deleted.
