# Needs your attention

What the maintainers need to **decide**, and the **critical or high-severity** issues to know.
It is the one place these are tracked, and it is kept current with every pull request, alongside
[`STATUS.md`](STATUS.md), which tracks what is built. Confidential detail (legal, IP, business
terms) stays in `docs-internal/` and is only referred to here.

**As of 2026-10-20.**

Severity: **Critical** blocks a milestone, or could cause harm if left. **High** should be
settled within the next few pull requests. An item leaves this page in the pull request that
settles it.

> **Security items are stated without exploit detail**, because this file becomes public with the
> repository. The repository must not go public while any security item below is open (part of
> the Gate 0 checklist); SECURITY.md's disclosure policy applies.

## Decisions only you can make

| # | Sev | Decision | Why it matters now | Blocks |
|---|---|---|---|---|
| D1 | Critical | **Gate 0: IP clearance** | Every week of building adds to what the clearance covers. The steps and their state are in `docs-internal/06-*`. | Going public, any release, PyPI, the container image, the docs site |
| D2 | Critical | **Two design-partner agents** | M1 and M2 cannot close without real agents; the GitHub Action has only ever gated the demo. | M1, M2 |
| D3 | High | **Human labels for judge calibration** (≥ 10 per judge) | `opseval calibrate` exists but has nothing to measure, so judge accuracy and false-pass rate are unknown. | M2 |
| D4 | High | **Product name / trademark** (D-06) | `opseval` is reserved on PyPI ahead of the decision; a rename costs more each week. | Launch, PyPI |
| D5 | High | **Telemetry collector and its data terms** (ADR-0010 decisions 2–3) | Telemetry is built but ships dark, so no KPI can be measured. | KPIs |
| D7 | High | **How parallel work is split** | Two sessions changed the server at once on 2026-10-08; one PR (#29) had to be closed as superseded. | — |

## Critical and high issues

None open as of 2026-10-20. The last one (I9, no concurrency) closed with the test-tenant pool
(ADR-0041). New ones are added here, numbered from I10, when they are found.

## For your information

- **Behaviour you may notice:** a flaky pass, a skip or an unmeasured check reads "No blocking
  issues, but …" (yellow), never "Safe to release" (ADR-0030, ADR-0032).
- **Behaviour you may notice:** every host Opseval contacts must be in `OPSEVAL_ALLOWED_HOSTS`,
  `127.0.0.1` included, and `opseval preflight` now exits 3 (not 2) when one is missing. A plugin
  sink publishes only when named in `OPSEVAL_SINKS`.
- **Parallel runs need a tenant pool:** `--lanes` without `--tenants` is refused, by design.
  Concurrency is bounded by isolated accounts: 20 concurrent attempts need 20 tenants and
  `OPSEVAL_LANE_CAP=20` (default 8). Leases are per-filesystem: CI runners with separate disks
  must share `OPSEVAL_TENANT_LOCK_DIR` (ADR-0041).
- **Behaviour you may notice:** every judge is now version 2 (ADR-0036). `opseval rescore` will
  not reuse an older judge score without a judge configured, and says so.
- **Behaviour you may notice:** Ctrl-C exits 130 (never 1). A profile with `committed_outcome`
  but no `commit_gate` no longer loads. A simulator call that takes longer than
  `OPSEVAL_SIMULATOR_TIMEOUT_SECONDS` (300 by default) invalidates the attempt as a harness error.
- **The console is not in the Python package yet**; that is a release-pipeline step for after
  Gate 0 (ADR-0034). It needs the Node versions in `packages/console/package.json` `engines`.
