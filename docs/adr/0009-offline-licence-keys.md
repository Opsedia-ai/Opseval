# ADR-0009: Offline-verifiable licence keys, no phone-home

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

Enterprise buyers in regulated environments (including in the GCC) run air-gapped. Licence checks must not require network access.

## Decision

Enterprise licence keys are **Ed25519-signed JSON** (org, edition, features, seats, expiry), checked against a public key compiled into the image. Keys are checked at startup and then hourly. After expiry there is a 30-day grace period, then EE features switch off. OSS features keep working, and no data is ever locked.

## Consequences

- No licence server is needed.
- Key revocation depends on expiry dates (short terms plus renewal).
