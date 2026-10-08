# Needs your attention

What the maintainers need to **decide**, and the **critical or high-severity** facts to know.
Kept current with every pull request, alongside [`STATUS.md`](STATUS.md), which tracks what is
built. Details that are confidential (legal, IP, business terms) stay in `docs-internal/` and are
only referred to here.

**As of 2026-10-08.**

Severity: **Critical** blocks a milestone or could cause harm if left. **High** should be settled
within the next few pull requests. An item leaves this page when it is settled; the pull request
that settles it says so.

## Decisions only you can make

| # | Sev | Decision | Why it matters now | What is needed | Blocks |
|---|---|---|---|---|---|
| D1 | Critical | **Gate 0: IP clearance** | The code base is growing (34 ADRs, about 1,000 tests) before the clearance that decides whether it can ship. Every week adds to what a negative opinion would put at risk. No clean-room evidence log exists yet. | Book the counsel review; start the per-component provenance log now (see `docs-internal/06-*`). | Going public, any release, PyPI publish |
| D2 | Critical | **Two design-partner agents** | M1 and M2 cannot close without real agents: the GitHub Action has only ever gated the demo. | Name two partners and their agents. | M1, M2 |
| D3 | High | **Human labels for judge calibration** | `opseval calibrate` exists, but there are no labels, so judge accuracy (and its false-pass rate) is unknown. | At least 10 labelled conversations per judge. | M2 |
| D4 | High | **Product name / trademark (D-06)** | `opseval` is reserved on PyPI ahead of the decision; a rename later costs more each week. | Trademark search, then a decision. | Launch, PyPI |
| D5 | High | **Telemetry collector and its data terms** (ADR-0010 decisions 2–3) | Telemetry is built but ships dark, so none of the KPIs can be measured. | Pick a collector and approve the data-processing terms, or decide to stay dark. | KPIs |
| D6 | High | **PR #25 (test-tenant pool)**: merge, rework or close | A v0.1 must-have (FR-RUN-08, FR-SAF-03). The branch is 2 commits ahead and 42 behind `main`, so it no longer merges as is. | Decide who owns it and whether it should be rebuilt on the current runner (which now writes progress and runs fail-fast through the gate policy). | v0.1 |
| D7 | High | **How parallel work is split** | Two sessions worked on the server at once on 2026-10-08; one PR (#29) had to be closed as superseded. | One owner per area (or per branch) while more than one session or contributor is active. | — |

## Critical and high issues known today

| # | Sev | Issue | Effect | Plan |
|---|---|---|---|---|
| I1 | High | A slow or rate-limited **simulator** uses up the agent's attempt deadline | The attempt ends as the agent's `timeout`: a harness problem drawn as an agent failure. | Fix next (core-rule work). |
| I2 | High | A keyless **LLM endpoint** skips the host allowlist | A judge or simulator URL without an API key can receive conversations from a host that is not allowlisted, over plain HTTP. | Fix next (security batch). |
| I3 | High | The **webhook URL** is stored and shown in plain text | Slack, Teams and Discord webhook URLs are credentials; this one reaches `result.json`, the report and `/config`. | Fix next (security batch). |
| I4 | High | **Release pipeline**: actions pinned to mutable tags, and `release.yml` publishes without running the checks | A moved tag or a broken commit can reach PyPI. | Fix before the first release (security batch). |
| I5 | High | Every installed `opseval.sinks` plugin is **enabled automatically** | Installing a package can start sending results somewhere without any configuration. | Make sinks opt-in (security batch). |
| I6 | High | An agent's text can **break out of the judge's transcript fence** | A hijacked agent could steer the judges that grade it. | Fence with a per-ballot nonce and quote checks (needs a scorer version bump). |
| I7 | High | A profile with `committed_outcome` but **no `commit_gate`** loads | The commit guard (ADR-0008) cannot stop such a scenario. | Refuse it at load time (core-rule work). |
| I8 | High | **Run control**: only `fail_fast` of five stop policies exists, and a run cannot be cancelled | A long run cannot be stopped from the console or on a rate-limit storm. | Build after the fixes above. |
| I9 | High | **No concurrency**: attempts run one at a time | Partner suites with repeats will be slow (the requirement is ≥ 20 concurrent attempts). | Belongs with D6 (lanes). |

## For your information

- **Behaviour you may notice**: a flaky pass, a skip or an unmeasured check is "No blocking
  issues, but …" (yellow), never "Safe to release" (ADR-0030, ADR-0032).
- **Local machine**: Homebrew's `node` is broken (a missing `simdjson` library); `brew reinstall
  node`, or use `/opt/homebrew/opt/node@24/bin`. The console needs Node 22.22+ or 24.15+.
- **The console is not in the Python package yet**; that is a release-pipeline step for after
  Gate 0 (ADR-0034).
