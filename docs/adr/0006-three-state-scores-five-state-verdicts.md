# ADR-0006: Three-state scores and five-state verdicts are a public contract

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

The main reason teams stop trusting eval results is harness noise (outages, expired keys, scorer bugs) reported as agent failure. The opposite failure, where real agent failures are hidden as "could not measure", is just as harmful.

## Decision

- Every score is `passed`, `failed` or `infra_error`. `score` is `null` if and only if the status is `infra_error`.
- An attempt is invalidated only for: transport failure, harness failure, a persona-consistency failure, all scores `infra_error`, or no agent-facing score.
- Test verdicts: `passed`, `failed`, `inconclusive` (fewer than ceil(repeats/2) valid attempts), `skipped_precondition`, `blocked`.
- CLI exit codes: `0` all passed, `1` any failed, `2` not judged / inconclusive / empty selection, `3` harness or configuration error.
- Unmeasured values are `null` and are rendered as "not measured", never 0.

## Consequences

- These are versioned public contracts. Changing them requires an RFC.
- A fault-injection test suite in CI proves that harness faults never produce `failed`.
