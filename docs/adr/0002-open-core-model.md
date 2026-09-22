# ADR-0002: Open-core model: Apache-2.0 core, `ee/` commercial modules, one codebase

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

We want broad adoption and a sustainable business. The model proven by Langfuse and others is: every product capability is permissively licensed, commercial modules are clearly separated, and every edition runs the same codebase.

## Decision

- The core is licensed **Apache-2.0**. We chose it over MIT for the explicit patent grant, which enterprise legal teams value.
- Enterprise modules live in `ee/` under the Opseval Enterprise License: source-available, and a licence key is needed to run them.
- OSS, Enterprise self-host and Cloud ship the **same container image and data schema**.
- The feature-split rule: if an individual engineer or a single team needs a feature to evaluate an agent well, it is open source. Features that only matter at organisation scale or for compliance go to Enterprise or Cloud.
- Pledge: released OSS features are never moved behind the key (see `GOVERNANCE.md`).

## Consequences

- CI enforces an import-linter contract: core must not import from `ee/`.
- Community PRs are not accepted into `ee/`, so the core never needs relicensing and DCO is enough (no CLA).
- Cloud-only services (billing, metering, signup) live in a separate private repository.
