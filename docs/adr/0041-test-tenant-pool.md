# ADR-0041: Test-tenant pool and parallel lanes

- **Status:** Proposed
- **Date:** 2026-10-05 (ported onto the current runner 2026-10-20)
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
prevent). The holder refreshes it from a **background task** every 30 seconds for as long as the
conversation runs, not once per finished attempt: one long attempt would never reach a
between-attempts refresh and would have its tenant taken mid-conversation. `release()` reads the
file back and refuses to delete it if someone else now holds it, so a slow process cannot release
its successor's lease.

The mechanism is a file created with `O_CREAT | O_EXCL`, which is atomic on POSIX and Windows. No
daemon, no lock server, and two Opseval processes on the same machine — the CI case — interlock
correctly. Cross-machine coordination would need shared storage and is not claimed.

#### Clearing a stale lease is itself a race

Unlinking a stale lease and re-creating it is **not** safe, and this is subtle enough to be worth
recording: two processes can both read the same stale lease, both decide it is free, both unlink,
and both then succeed at `O_EXCL` — the double-hold the whole module exists to prevent, reintroduced
by the recovery path. `os.rename` to a unique name is the atomic step instead. Exactly one process
can move a given file, so exactly one gets to clear it; the winner must still win `O_EXCL`
afterwards, so a third process creating the lease in the gap costs us the tenant rather than the
lock.

#### A crash must not retire a tenant

The heartbeat is written to a temporary file and `os.replace`d, because a truncating write that is
interrupted leaves an **empty** lease — and "unreadable means held" would then make that tenant
unusable forever rather than for five minutes. For the same reason, when no trustworthy heartbeat
can be read the file's own mtime decides, and only an unreadable *age* counts as held.

`bool` is an `int` and `json` parses a bare `NaN`, so `True` would age to 1970 and every `NaN`
comparison is `False`: both would read as *stale* and get a live tenant stolen. Neither is a
timestamp, and neither is accepted as one.

#### Transient contention is not a skip

`acquire` waits up to `OPSEVAL_TENANT_WAIT_SECONDS` (30 s) for a free tenant before giving up. One
leftover lease from a killed run would otherwise skip a scenario that could have run a second later
— and with every scenario skipped the gate now blocks the release over a purely harness-side
condition. Waiting absorbs contention; it never relaxes the lock.

#### The lock lives where the accounts do, not where the checkout does

The default lock directory is machine-wide and named after a digest of the pool's tenant ids, not a
path relative to the working directory. A relative path resolves to two different directories when a
CI runner checks the same repository out twice, so both jobs would believe the pool was free and
both would lease the same account — the failure the lock exists to prevent, reappearing because of
*where the lock lives*. The shared resource is the set of accounts, so that is what the directory is
named after. `OPSEVAL_TENANT_LOCK_DIR` overrides it, which is how to put the locks on shared storage.

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
a reason that has nothing to do with the agent. A tenant's own token beats both
`OPSEVAL_STATIC_TOKEN` and a minted one, because the whole point of the pool is that each tenant
authenticates as itself.

Resolution goes through `Settings.secret_named`, not `os.environ`. That keeps the "only `config`
reads the environment" rule, and it fixes a trap worth naming: **`.env` is read into `Settings` and
never exported to the process environment.** A token set exactly where `opseval init` says to put it
would otherwise look unset, and every run would stop at exit 3.

#### A minted token cannot carry a tenant

A profile with a `token:` block mints HS256 tokens from the profile's own claims and knows nothing
about tenants. Combined with a pool it would hand every lane the *same* identity while the operator
believed the accounts were isolated — the shared-state corruption the pool exists to prevent, with
the harness recording the result as agent misbehaviour. So a minting profile plus a tenant with no
`token_env` is refused at launch, naming the fix. Rather than inventing a claim name to inject the
tenant into, the operator says which variable holds each tenant's token.

#### Every tenant is checked before the first frame

The pool is loaded and *all* of its tokens and targets validated next to the sinks, before preflight
and before the manifest is written. Checking lazily, as each tenant is first drawn, fails the run
mid-suite after records are already on disk — and with one lane the later tenants may never be drawn
at all, so the pool would silently degrade to a single account with nothing said. "Authoring
mistakes must fail at load time" applies to the pool too.

### Run control with lanes

Every stop policy (ADR-0035) and a cancel request work with lanes. A lane checks them when it is
about to start a scenario, so a policy that fires stops **new** scenarios from starting; it cannot
un-start scenarios already running, which finish. A cancel is still checked before every attempt.
Whatever did not start is a `skipped_precondition` verdict naming the reason, and `result.tests`
keeps catalogue order, not completion order. "Consecutive" failures are counted in completion
order, which is the only order lanes have.

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
  whatever the lock directory is named: point `OPSEVAL_TENANT_LOCK_DIR` at shared storage, or keep
  the pool with the runner. A database- or Redis-backed lease is the obvious extension and is not
  built.
- A crashed run holds its tenants for up to five minutes (minus the wait, so a scenario sees that as
  a delay of `tenant_wait_seconds` and then a skip). That is the cost of the asymmetry above;
  `OPSEVAL_TENANT_STALE_AFTER_SECONDS` lowers it for anyone who would rather risk the other failure.
- There is no `opseval` command to list or clear leases. The lock directory is printed at the start
  of a pooled run, and the files are plain JSON, but recovery below `stale_after` means deleting
  them by hand.
- The lease is held across trace enrichment and scoring as well as the conversation, although
  neither touches the tenant. This costs *this* run nothing — lanes never exceed the pool, so every
  lane always has a tenant to hold and the lane count is the bottleneck, not the lease. It only
  matters to a **second** process sharing the same pool, which waits longer than it strictly needs
  to. Releasing at the end of the last attempt would fix that.
- `python-dotenv` is now a declared dependency rather than one inherited from `pydantic-settings`,
  because `Settings.secret_named` reads `.env` directly.
- Scenario output is interleaved while lanes run, so the console prints the tenant with each
  scenario. The ordering of `result.tests` still follows the catalogue, not completion.
- `tenants.yaml` is now a published contract (`schemas/v1/tenants.json`, ADR-0026) and `opseval
  init` scaffolds a commented example, so the shape — and the rule that tokens stay out of the file
  — is discoverable without reading this ADR.

## Amendment (2026-10-20): the port, and a heartbeat race

- The pool was built when the runner had no progress log, gate policy, stop policies,
  cancellation or cut-short folding. It is now ported onto that runner: one `run_one` per
  scenario holds every one of those rules, the sequential path is unchanged, and lanes are an
  `asyncio.Semaphore` over it. Injected transports (tests, the pytest plugin) keep their
  one-argument factory; a pool with an injected transport is refused, since it has no tenant
  identity to apply. ADR-0030 had already made a suite that judged nothing block, so the gate
  change this pool originally carried was not needed.
- **A heartbeat could land after its lease was released.** The heartbeat is written in a thread,
  and cancelling the task that awaits it does not stop the thread: a write already under way
  finished after `release()` deleted the lease, re-creating it, and the released tenant looked busy
  until it went stale (five minutes). Each write is now shielded and awaited before the refresh
  block returns, and a heartbeat never re-creates a lease file that is gone. Found as a test that
  failed only under load.
- Verified end to end: two separate processes, three lanes each, sharing one three-tenant pool
  against the reference agent. Both runs passed, one tenant served three scenarios in strict
  sequence across the two processes with no overlap, and every lease was released.

## Amendment (2026-10-20): what the port's review closed

Review of the port (#62) found three ways two runs could still end up on one tenant. Each is closed
and tested:

- **A reclaimer could move a fresh lease.** `os.rename` moves whatever is at the path *now*, which
  may already be the lease another reclaimer just took. Every lease file now carries its own
  `lease_id`. After renaming, the reclaimer checks it moved the stale lease it judged; if not, it
  puts the file back with a no-clobber `os.link` and gives up.
- **The heartbeat only ran during the conversation**, not during enrichment, scoring or judging, and
  short attempts never refreshed at all. The refresher now covers the whole hold, from acquiring
  the lease to releasing it.
- **A late heartbeat could stamp itself over another run's lease.** A heartbeat now writes only while
  the file still carries its own `lease_id`. Otherwise the lease is marked **lost**, and an attempt
  run under a lost lease is recorded with a harness error and is *not judged*, never a verdict.

Also:

- `stale_after` must be at least three heartbeats (90 s), or every live lease would look dead.
- Lanes run in a `TaskGroup`: if one fails, the others are cancelled and release their leases, and
  the first error is re-raised as itself (a `ConfigError` stays exit 3).
- Tenants that are one account under two names are refused: neither `token_env` nor `target`, or
  the same of both.
- Every distinct tenant target is preflighted, and the result's `target` names the hosts attempts
  actually reached.
- Progress tracks every running scenario. A scenario still waiting for a tenant is not shown as
  running.
- Tenant tokens are read from the `.env` file the `Settings` instance was actually built with
  (`_env_file`), parsed once.

