# ADR-0029: Test-tenant pool and parallel lanes

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Opseval maintainers

## Context

FR-RUN-08 asks for a pool of test tenants with `lanes = min(tenants, requested, cap)`, and
FR-SAF-03 asks that destructive scenarios only ever run against an isolated test account. They are
the same feature seen from two sides: the pool is what makes parallelism safe, and parallelism is
what makes the pool worth having.

Running scenarios one at a time is correct but slow, and a suite that takes twenty minutes does not
get run on every pull request. The obvious speed-up — run them concurrently — is unsafe against a
shared account: two attempts refunding from the same order list see each other's state, and the
resulting mess is recorded as *agent misbehaviour*. That is the project's one rule inverted. The
harness would be manufacturing agent failures.

## Decision

A declared pool of tenants, one exclusive lease per scenario for the length of its attempt, and
lanes bounded by the pool.

### The lock errs towards not running

This is the decision everything else follows from:

> **When in doubt, do not take the lease.**

A scenario that cannot get a tenant is `skipped_precondition` — the harness could not obtain a
resource, which is not the agent's fault, and the exit code is 2, not 1. That outcome is mildly
annoying. Two runs sharing a tenant produces *false agent failures*, which is much worse and much
harder to diagnose. So the asymmetry is deliberate and lives in `_held_by_other`: a lease that
cannot be read, or has no usable heartbeat, counts as **held**, not free. Guessing "free" is exactly
how two runs end up on one account.

A lease is only ever reclaimed when its heartbeat is provably older than `stale_after` (default 300
seconds — generously long, because reclaiming a live lease is the failure this module exists to
prevent). The holder refreshes it every 30 seconds while it runs, and `release()` reads the file
back and refuses to delete it if someone else now holds it, so a slow process cannot release its
successor's lease.

The mechanism is a file created with `O_CREAT | O_EXCL`, which is atomic on POSIX and Windows. No
daemon, no lock server, and two Opseval processes on the same machine — the CI case — interlock
correctly. Cross-machine coordination would need shared storage and is not claimed.

### Lanes cannot exceed the pool

`lane_count(accounts, requested, cap)` is `min` of the three, never below 1. A lane with no tenant
to hold would defeat the isolation the pool exists for, so the number of accounts is a hard ceiling
however many lanes were asked for: `--lanes 9` against three tenants gives three lanes, and the
console says so rather than silently doing something else.

Without `--tenants` the run is sequential with no leases and no behaviour change. Parallelism is
opt-in *through* the safety mechanism, which is the point: there is no way to ask for concurrency
without first saying which isolated accounts it may use.

### Credentials are named, never stored

A pool file is meant to be committed and reviewed, so each tenant carries `token_env` — the *name*
of the environment variable holding its token — and never the token. A declared variable that is
unset is a `ConfigError` (exit 3) rather than an attempt that authenticates as nobody and fails for
a reason that has nothing to do with the agent. A tenant's own token beats `OPSEVAL_STATIC_TOKEN`,
because the whole point of the pool is that each tenant authenticates as itself.

Per-tenant `target` is checked against `OPSEVAL_ALLOWED_HOSTS` when it is used, and the default
target is still validated eagerly before the first frame, so a typo in the common case fails
immediately instead of after a suite has started.

### Fail-fast is honest about what it cannot do

With lanes in flight, `--fail-fast` cannot un-start the attempts already running. It stops new ones
from starting and says that, rather than implying it cancelled work it did not cancel.

## Consequences

- A suite runs as many scenarios at once as there are isolated accounts, which is what makes a
  large catalogue viable on every pull request. Verified end to end: three tenants across three
  lanes, one distinct tenant each, every lease released afterwards.
- **An exhausted pool blocks rather than fails.** With every lease held, all scenarios came back
  `skipped_precondition` at exit 2 — and the gate now says "nothing was measured: all 4 scenario(s)
  were skipped or not judged" instead of "Safe to release". Building this surfaced that bug in
  `release_gate`: a suite in which nothing was measured was reporting a pass. Fixed here, because a
  pool is the easiest way to reach that state by accident.
- Leases are per-filesystem. Two CI runners with separate disks will both believe the pool is free,
  so the pool file belongs with the runner, or the lock directory on shared storage. A database- or
  Redis-backed lease is the obvious extension and is not built.
- A crashed run holds its tenants for up to five minutes. That is the cost of the asymmetry above;
  `OPSEVAL_TENANT_STALE_AFTER_SECONDS` lowers it for anyone who would rather risk the other failure.
- Scenario output is interleaved while lanes run, so the console prints the tenant with each
  scenario. The ordering of `result.tests` still follows the catalogue, not completion.
- `tenants.yaml` is now a published contract (`schemas/v1/tenants.json`, ADR-0026) and `opseval
  init` scaffolds a commented example, so the shape — and the rule that tokens stay out of the file
  — is discoverable without reading this ADR.
