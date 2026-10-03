# ADR-0022: Scorer plugin API and contract test kit

- **Status:** Proposed
- **Date:** 2026-10-03
- **Deciders:** Opseval maintainers

## Context

FR-SCR-10 asks for a scorer plugin API over Python entry points, with a contract test kit, and
third-party scorers listed by `opseval list-scorers`. The architecture fixes the entry-point group
as `opseval.scorers` and the kit's home as `opseval.testing`.

The interesting question is not how to load a class. It is what to do when a plugin is wrong, and
what the kit may honestly claim to verify.

## Decision

### Discovery fails closed, and loudly

A plugin that cannot be imported, does not satisfy the contract, or collides with an existing
scorer id raises `ConfigError` → **exit 3**. It is never skipped, and one broken plugin stops the
whole discovery rather than leaving a partial registry.

That is deliberate, and it is the same trap this project has now hit three times. Skipping a
plugin looks harmless: the suite just wouldn't find that scorer. But a suite naming a missing
scorer gets `UNKNOWN_SCORER`, which is an `infra_error`, and `attempt_verdict` **drops
`infra_error` from the measured set**. So a scenario with one other passing scorer would report
**passed, exit 0**, with the plugin's check silently gone — a security scorer could vanish and the
gate would stay green. A broken plugin must stop the run, not quietly narrow it.

Discovery is idempotent, so importing Opseval from several places cannot trip the duplicate-id
guard, and it runs at the start of `launch()` — before the catalogue is validated, so a suite may
name a plugin's scorer and a broken plugin is reported as a plugin problem rather than surfacing
later as an unknown id.

### The contract is checked, not trusted

`check_contract` verifies the class derives from `Scorer`, has a non-empty `id`, a real `Axis`, a
`requires` of `frames` / `trace` / `llm`, an `async def score`, and a non-empty `version`. A
synchronous `score` is rejected rather than awaited by accident.

A collision error tells the author to namespace their id, and `list-scorers` marks every
third-party scorer with the distribution that provides it, so an operator can see what is
installed and where a scorer came from.

### The kit claims only what it can find

This is the part worth being careful about. `ScoreResult` **already validates** the three-state
invariants at construction: `score is None` exactly when the status is `infra_error`, a score
within `[0, 1]`, and a reason on every `infra_error`. A scorer cannot return a `ScoreResult` that
breaks them, so a kit check asserting those finds nothing. Shipping them as "the contract kit
validates three-state behaviour" would overstate it.

What the kit actually finds is what the model cannot see:

- the scorer **raises** instead of reporting. The engine converts an exception into `infra_error`,
  so the bug produces a plausible result and nobody investigates;
- it returns something that is **not** a `ScoreResult`;
- it **mutates** the record. Frozen models block attribute assignment, but a dict inside a frame
  payload is reachable, and a later scorer, the report and `opseval rescore` all read that same
  evidence;
- it reports **another scorer's** id or axis;
- it declares no `version`, so `rescore` cannot attribute a moved verdict;
- its id is not namespaced, so it will collide eventually.

The state assertions are kept as regression guards — they would catch the model loosening, or a
scorer bypassing it — and the kit's docstring says so rather than implying they are discoveries.

An assertion that is *not* a universal contract was removed during review: "missing evidence is
not a failure". A record with nothing in it legitimately fails some scorers —
`output.terminal_state` is one — so asserting otherwise would have forced plugin authors to break
a rule the built-ins do not follow.

Verified end to end with a real installed package, not a stub: a two-line test file
(`class TestX(ScorerContract): scorer = ToneScorer`) runs eleven checks, and
`opseval list-scorers` shows `acme.tone … [acme-opseval]`.

## Consequences

- Third parties can ship scorers and prove compliance in their own CI without importing anything
  private. `opseval.testing` is now a public surface and is covered by the v1 stability promise
  from v0.2 on, so changing a kit assertion can break somebody's build.
- A broken or stale plugin makes `opseval run` and `opseval list-scorers` exit 3 until it is fixed
  or uninstalled. That is the intended trade, and it means an abandoned plugin blocks upgrades
  until removed.
- `opseval.testing` imports `pytest`, which is a dev dependency, so importing it outside a test
  environment fails. It is only ever imported from a test suite, and bandit is scoped past its
  assertions, which are its product rather than a hazard.
- Plugins run **in-process with no sandbox**. A scorer is arbitrary code with the harness's
  credentials and network access; installing one is as much a trust decision as installing any
  dependency. Nothing here changes that, and the contract kit does not make a plugin safe — it
  only makes a *badly behaved* one visible.
- Only the scorer group is implemented. The architecture lists eight more (`opseval.transports`,
  `opseval.langpacks`, `opseval.trace_sources`, `opseval.sinks`, `opseval.credentials`,
  `opseval.cleanup`). `plugins.py` is written so the group is a parameter, but each needs its own
  contract and its own kit.
