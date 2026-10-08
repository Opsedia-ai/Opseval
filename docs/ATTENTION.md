# Needs your attention

What the maintainers need to **decide**, and the **critical or high-severity** issues to know.
It is the one place these are tracked, and it is kept current with every pull request, alongside
[`STATUS.md`](STATUS.md), which tracks what is built. Confidential detail (legal, IP, business
terms) stays in `docs-internal/` and is only referred to here.

**As of 2026-10-08.**

Severity: **Critical** blocks a milestone, or could cause harm if left. **High** should be
settled within the next few pull requests. An item leaves this page in the pull request that
settles it.

> **Security items are stated without exploit detail**, because this file becomes public with the
> repository. The repository must not go public while any security item below is open (part of
> the Gate 0 checklist); SECURITY.md's disclosure policy applies.

## Decisions only you can make

| # | Sev | Decision | Why it matters now | Blocks |
|---|---|---|---|---|
| D1 | Critical | **Gate 0: IP clearance** | Every week of building adds to what the clearance covers. The steps and their state are in `docs-internal/06-*`. | Going public, any release, PyPI |
| D2 | Critical | **Two design-partner agents** | M1 and M2 cannot close without real agents; the GitHub Action has only ever gated the demo. | M1, M2 |
| D3 | High | **Human labels for judge calibration** (≥ 10 per judge) | `opseval calibrate` exists but has nothing to measure, so judge accuracy and false-pass rate are unknown. | M2 |
| D4 | High | **Product name / trademark** (D-06) | `opseval` is reserved on PyPI ahead of the decision; a rename costs more each week. | Launch, PyPI |
| D5 | High | **Telemetry collector and its data terms** (ADR-0010 decisions 2–3) | Telemetry is built but ships dark, so no KPI can be measured. | KPIs |
| D6 | High | **PR #25 (test-tenant pool)**: merge, rework or close | A v0.1 must-have (FR-RUN-08, FR-SAF-03). The branch is far behind `main` and predates the progress log and the policy-aware fail-fast, so it no longer merges as is. | v0.1, concurrency |
| D7 | High | **How parallel work is split** | Two sessions changed the server at once on 2026-10-08; one PR (#29) had to be closed as superseded. | — |

## Critical and high issues

| # | Sev | Issue | Effect | Plan |
|---|---|---|---|---|
| I1 | High | Time spent waiting on the simulator counts against the agent's attempt deadline | A harness slowdown can end an attempt as the agent's timeout (the core rule). | Next: core-rule fixes |
| I6 | High | The judges' handling of agent text can be strengthened (security) | A misbehaving agent could influence the judges that grade it. | After the security batch (needs a scorer version bump) |
| I7 | High | The commit guard does not cover every profile shape | A scenario could reach the irreversible outcome without the double opt-in (ADR-0008). | Next: core-rule fixes |
| I8 | High | Run control: four of five stop policies are missing, and runs cannot be cancelled | A long or failing run cannot be stopped from the console. | After the fixes above |
| I9 | High | No concurrency: attempts run one at a time | Partner suites with repeats will be slow (the requirement is ≥ 20 concurrent attempts). | With D6 (lanes) |

## For your information

- **Behaviour you may notice:** a flaky pass, a skip or an unmeasured check reads "No blocking
  issues, but …" (yellow), never "Safe to release" (ADR-0030, ADR-0032).
- **The console is not in the Python package yet**; that is a release-pipeline step for after
  Gate 0 (ADR-0034). It needs the Node versions in `packages/console/package.json` `engines`.
