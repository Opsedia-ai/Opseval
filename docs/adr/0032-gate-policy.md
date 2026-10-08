# ADR-0032: Gate policy: teams decide what each outcome does, never what it is

- **Status:** Proposed
- **Date:** 2026-10-08
- **Deciders:** Opseval maintainers

## Context

Until now, the release gate's rules were fixed in `release_gate`:

- a blocker or critical failure blocks, and a normal failure warns;
- a skip warns;
- an inconclusive scenario blocks;
- a flaky scenario warns, unless `OPSEVAL_GATE_FAIL_ON_FLAKY` makes it block.

Teams need different rules for different suites. A release branch wants everything to block.
A nightly exploratory suite wants only critical failures to matter. A security suite wants an
unmeasured judge to block, because a missing security verdict is not a pass. ADR-0030 had
already found one case the fixed rules could not handle: a skip can be a dependency outage or
the operator's own choice (the commit guard), and the gate could not tell them apart.

Making "everything configurable" has a trap. The value of an Opseval verdict is that it means
the same thing in every report. If a setting could turn "the simulator crashed" into "the
agent failed", the one rule would become an option, and no result could be trusted without
first asking which settings produced it.

## Decision

**Split facts from policy.**

- **Facts stay fixed in code.** These are what happened: a score is `passed | failed |
  infra_error`; a timeout is the agent's or the harness's; an unmeasured value is not 0.
- **Policy is configurable.** It decides what each fact does to the release: `block`, `warn`
  or `ignore`.

**Where it lives.** The policy is a `gate:` block in the suite YAML. It is versioned with the
suite in git, so the CLI, CI and the console all apply the same rules:

```yaml
gate:
  preset: strict            # strict | standard | lenient
  failed: {normal: warn}    # any setting here overrides the preset
  unmeasured: block
```

**Settings and their allowed actions:**

| Setting | Allowed | Notes |
|---|---|---|
| `failed.{blocker,critical,normal}` | block, warn, ignore | |
| `skipped.{blocker,critical,normal}` | block, warn, ignore | dependency or simulator outages |
| `commit_guard` | warn, ignore | Never `block`: that would push teams to `--allow-commit` (ADR-0008) |
| `quarantined` | block, warn, ignore | scenarios tagged `blocked` |
| `inconclusive` | block, warn | Never `ignore`: an unjudged scenario cannot vanish |
| `flaky`, `unmeasured`, `degraded`, `vacuous` | block, warn, ignore | |
| `load_errors` | block, warn | |
| `undeclared_criticality` | critical, normal | |
| `exit_code` | strict, gate | see below |

**Presets.** `standard` is the previous behaviour. `strict` blocks everything except the
commit guard. `lenient` blocks only blocker and critical failures, and lets the gate decide
the exit code.

**Overrides and the old switch.**

- `opseval run --gate PRESET` replaces the suite's policy whole, so a release branch asking
  for `strict` gets exactly `strict`.
- `opseval rescore --gate PRESET` re-gates a saved run offline. By default, rescore re-gates
  under the run's own recorded policy.
- `OPSEVAL_GATE_FAIL_ON_FLAKY` still works, as `flaky=block`.

**Skips carry their reason.** `TestVerdict.skip_kind` is `dependency | simulator |
commit_guard | quarantine`, so the policy can treat an outage and a deliberate skip
differently.

**Recorded and named.**

- The resolved policy is stored on `Gate.policy` in every result.
- Every output says which policy was used, as `standard` or `strict + flaky=warn`.
- Every output lists each setting looser than `standard` ("⚠ looser than standard").
- What the policy ignored is listed in `Gate.ignored`. The summary names whatever is ignored
  beyond what `standard` already ignores.
- A lenient green can therefore never pass for a strict one.

**Not configurable:**

- A suite where nothing was judged always blocks.
- Exit 3 (every attempt was a harness failure) is always 3.
- "Safe to release" (`Gate.safe`) needs nothing blocking and nothing warning. Under
  `exit_code: strict` it also needs every scenario to have passed and loaded, so it never
  sits next to a non-zero exit code.

**Exit codes.**

- `strict` (the default): codes say what happened (any failure 1, anything unjudged 2),
  whatever the gate chose.
- `gate`: codes follow the decision (pass 0; blocked by agent behaviour 1, from
  `Gate.agent_blocked`; blocked by what could not be judged 2).

## Consequences

- Under `standard`, the behaviour is the same as before with one exception. A **flaky**
  scenario that passed used to read "Safe to release". It now reads "No blocking issues, but
  not everything passed" (`warn` is a gap). A flaky critical scenario is something a release
  manager should see.
- `result.json` gains `gate.policy`, `gate.ignored`, `gate.agent_blocked`, `gate.safe` and
  `tests[].skip_kind`, and `suite.json` gains `gate`. The published schemas are regenerated.
  Results from before this change still read, and `safe_to_release` falls back to the
  ADR-0030 rule for them.
- The console server reads `Gate.agent_blocked` instead of parsing the blocking strings. Run
  detail shows the policy and any setting looser than standard. Editing the policy from the
  console is the next step, behind the authenticated API of ADR-0031.
- pytest-opseval still maps verdicts to pytest outcomes one test at a time. Following the
  suite's gate policy there is future work.
