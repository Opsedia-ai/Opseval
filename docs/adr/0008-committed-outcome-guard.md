# ADR-0008: Committed-outcome guard is on by default

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

End-to-end tests really run the agent's tools. An open-source tool that can accidentally issue a real refund or transfer is a safety and reputational risk.

## Decision

Opseval refuses to answer the final checkpoint before a profile's committed (irreversible) outcome unless **both** conditions hold: the scenario sets `allow_commit: true` **and** the run is started with `--allow-commit`. This is a v0.1 launch blocker.

## Consequences

- Scenarios that need the full outcome must opt in twice, deliberately.
- Documentation stresses using dedicated test tenants.
