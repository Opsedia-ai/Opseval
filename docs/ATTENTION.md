# Needs your attention

What the maintainers need to **decide**, and the **critical or high-severity** issues to know.
It is the one place these are tracked, and it is kept current with every pull request, alongside
[`STATUS.md`](STATUS.md), which tracks what is built. Confidential detail (legal, IP, business
terms) stays in `docs-internal/` and is only referred to here.

**As of 2026-10-28** (the roadmap of 2026-10-28: tasks, accounts, analytics and dashboards, voice, CI/CD anywhere, the flexibility ladder and protecting `main`, ADRs 0049–0066).

Severity: **Critical** blocks a milestone, or could cause harm if left. **High** should be
settled within the next few pull requests. An item leaves this page in the pull request that
settles it. Each item carries a recommendation; the reasoning behind them is the adoption review
of 2026-10-20 (four kinds of agent onboarded by hand, a code audit, the planning documents),
kept in `docs-internal/review-2026-10-20-any-team-adoption.md` because it covers business terms.

> **Security items are stated without exploit detail**, because this file becomes public with the
> repository. The repository must not go public while any security item below is open (part of
> the Gate 0 checklist); SECURITY.md's disclosure policy applies.

## Decisions only you can make

| # | Sev | Decision | Why it matters now | Blocks | Recommendation |
|---|---|---|---|---|---|
| D1 | Critical | **Gate 0: IP clearance** | Every week of building adds to what the clearance covers. The steps and their state are in `docs-internal/06-*`. | Going public, any release, PyPI, the container image, the docs site | Book the counsel consultation now. The open steps, and the questions to take to counsel, are in the review (section 7) and `docs-internal/06-*`. |
| D2 | Critical | **Two design-partner agents** | M1 and M2 cannot close without real agents; the GitHub Action has only ever gated the demo. | M1, M2 | Pick at least one partner whose agent has its own HTTP API, not an OpenAI-compatible one: that is the case the PRD's goal G2 promises and the review found unmet (I16). Offer to write its first profile together. |
| D3 | High | **Human labels for judge calibration** (≥ 10 per judge) | `opseval calibrate` exists but has nothing to measure, so judge accuracy and false-pass rate are unknown. | M2 | Label 10 attempts per judge from runs against the reference agent and the first partner agent, and read the false-pass rate before the overall agreement. |
| D4 | High | **Product name / trademark** (D-06) | `opseval` is reserved on PyPI ahead of the decision; a rename costs more each week. | Launch, PyPI | Run the trademark search before the docs site or any public post uses the name, and record the outcome in a `TRADEMARKS.md` (doc 06 lists it; it does not exist yet). Reserve every package name the README tells people to install, and `opseval-ee`, on PyPI now (I30), and mark the README's install lines "after the first release". |
| D5 | High | **Telemetry collector and its data terms** (ADR-0010 decisions 2–3) | Telemetry is built but ships dark, so no KPI can be measured. | KPIs | Keep it dark until Gate 0. Until then, measure onboarding by hand: time to a first run against the team's own agent, on four agent shapes (D9). |
| D7 | High | **How parallel work is split** | Two sessions changed the server at once on 2026-10-08; one PR (#29) had to be closed as superseded. | — | Keep the split agreed on 2026-10-20: one session holds the driver, transports, profile and scenario models, catalogue, scoring, simulator and scaffold; the other holds sinks, auth, plugins and traces. ADR numbers are claimed before writing, and every branch is rebased just before it is pushed. |
| D8 | High | **Merge rights for the adoption work** | #65 was merged by hand. The PRs D12 lists touch the same files in sequence; each waits on a merge. | Pace of D12 | Let the session merge its own adoption PRs once CI is green and the review's findings are addressed, as was allowed for the console. |
| D9 | High | **Plain chat as the way in** | Most teams arrive with a chat or RAG agent and add tools and approvals later; how the vision document treats plain-chat-only use needs revisiting (`docs-internal`, doc 01 §7). | Docs, quickstart, D12 | Treat plain chat as the way in and approval checkpoints as the differentiator. Make G2 measurable: four reference agents (HTTP JSON, OpenAI-compatible, in-process Python, tool-using with confirmation) onboarded by configuration alone, timed each quarter. |
| D10 | High | **Unmeasured checks on critical scenarios** | Under the `standard` gate, a critical security scenario with no judge configured is *not measured*, a warning: CI exits 0 on a security test that never ran. | Any CI that relies on security scenarios | Now: have the `full` suite that `opseval init` writes declare `gate: {unmeasured: block}`. Later, decide whether `standard` itself blocks an unmeasured check on a critical or blocker scenario. |
| D11 | High | **What frames can prove about tools** | Without traces, tool checks read tool calls from frames. An agent that runs a tool on its server never shows it, so a correct agent fails `tools.expected_called` (I11). | I11 | **Implemented as recommended in ADR-0043** (`tool_calls_complete`, false by default; the reference agent's profile sets it). Reversible: say so if you want frames to count absence as a failure by default. Frames can prove that a tool ran, never that it did not. A missing tool is *not measured* unless the profile declares `tool_calls_complete: true`, which the OpenAI-compatible scaffold sets for raw model endpoints. |
| D12 | High | **Order of work** | v0.5 server work (Postgres, workers, accounts) and the adoption gaps compete for the same weeks. M1 needs a real partner agent onboarded (D2). | M1 | **Owner decision (2026-10-21): server mode on Postgres starts now (ADR-0047)**; its first step, the versioned catalogue, is built. Recommendation for the rest: alternate ADR-0047's steps with the adoption steps, because they depend on each other: (1) profile-aware defaults and a check that every turn got a reply (I10), and check settings described (I15), which the scenario form needs; (2) the core-rule fixes I11–I13, I27 and I28; (2b) hardening against a hostile agent, I25, I26 and I29, before the repository goes public; then the console on the database catalogue with the scenario form (D14), with I19 and I20; (3) mapping any request and reply (I16); then runs in the database, accounts and API keys, and workers (ADR-0047); (4) content checks from YAML and custom judge criteria; (5) environments and `init` templates for every protocol; (6) any persona language; (7) data-driven scenarios. |
| D13 | High | **Updating the planning documents** | The architecture document still describes server mode and a repository layout that do not exist, and nine plugin groups of which three exist; the PRD has no row for onboarding an agent by configuration. | Specs for D12 | Issue doc 05 v0.2 with a built / partial / planned column per component, and add the PRD rows the review lists (section 4). Accept ADRs as the epic specs in doc 09, or write the specs. |
| D14 | High | **Scenarios added mainly from the console** | Today a scenario is added by writing YAML, in a file or in the console's text editor. The people who know what to test (QA, conversation designers, product owners) should not need YAML. | D12, FR-AUT | **Storage decided by the owner (2026-10-21): the database, on the Langfuse model (ADR-0047).** Scenarios, suites and profiles live in Postgres with immutable versions and labels; a folder stays the import and export format (`opseval catalogue push` / `pull`) for Git and for CI with no server. Recommendation for the form itself, unchanged: a guided form in the console (goal, simulated customer, conversation, checkpoint answers, tools, policy rules, checks, criticality, repeats) whose choices come from the profile, started from a template, a copy or a rehearsed conversation; the server builds and validates everything, and a save is a new version. It is ADR-0047's next step and needs I15 and D12 step 1 first, and I19 and I20 so that a scenario added in the console can be run from it. |
| D15 | High | **Voice in scope, MENA first** | The owner made voice evaluation the top priority on 2026-10-28; the PRD lists voice as a v1 non-goal and the risk register calls it scope creep. Voice adds paid speech and telephony providers, inbound public endpoints for phone calls, and audio that is personal (biometric) data under GDPR, Saudi PDPL, UAE PDPL and China's PIPL. | Voice work (ADRs 0053–0057), PRD rows | Update the PRD and risk register first (with D13), and pick the speech providers and the data region for a MENA design partner (D2) before phone calls (ADR-0056). Start with the two transports that need no public endpoint (HTTP audio and WebSocket audio, ADR-0053), local mode only, synthetic callers only. |
| D16 | High | **Order of the 2026-10-28 roadmap** | Voice, tasks, accounts, analytics, dashboards, CI/CD and the flexibility ladder all compete with the D12 adoption steps and the going-public items. | D12 | Recommended order: re-land the guided form (#74), protect `main` softly and refuse unknown languages (ADR-0065, ADR-0059); the voice foundations (ADRs 0053–0054) with the GitHub drag-and-drop examples (ADR-0058, E1); then tasks and runs in the database (ADRs 0049, 0051) with the cost and token dashboards (ADR-0066); then accounts (ADR-0050), CI server reporting and other CI templates, analytics; then the MENA language packs and the L1/L2 ladder; then phone calls, workers, L3/L4, WebRTC and L5. |

## Critical and high issues

| # | Sev | Issue | Effect | Plan |
|---|---|---|---|---|
| I13 | High | Every HTTP 4xx from an agent is a harness error on http-json and sse | An agent's own refusal (400, 422) is hidden as *not judged*, and a 429 is not marked as a rate limit, so `stop_on_rate_limit` never fires there (openai-chat already handles 429). | D12 step 2: one HTTP status policy for every HTTP transport, 429 retried and marked, agent statuses declarable in the profile |
| I14 | High | A persona language Opseval has no pack for becomes English | The simulated customer writes English for `fr` or `de`; the persona judge can then invalidate attempts until the scenario is inconclusive, so the harness blocks a release. `validate` says nothing. | Refused at load from the first roadmap step: an unknown language is a load error, a dialect falls back only where its pack allows it, with a warning (ADR-0059) |
| I16 | High | A team's own HTTP API often cannot be mapped by configuration | Reply fields must be top-level keys; there are no custom headers (no API-key header), no `"Bearer {token}"`-style templates, no per-attempt ids, no streaming joined into one turn. In the review, one of four agent shapes onboarded by configuration alone; one had to change its API in four ways. | D12 step 3 (ADR-0044) |
| I17 | High | The console serves whatever build is on disk, however old | A build from before today's run states draws a blocked or a safe run as *not judged*. Nothing checks that the console and the API agree. | Stamp the build with a hash of the API types; the server refuses a build that does not match |
| I18 | High | Compare can say "nothing regressed" when it should not | A newer run that judged nothing, or whose new scenarios fail, reads green; a scenario dropped from the suite is invisible. The headline is decided in the browser. | Added and removed scenarios in the comparison, and a server-decided result: regressed, no regression, or not comparable |
| I19 | High | The console can only run websocket agents | The launch form has no transport, and `serve` has none either, so even the catalogue `opseval init` writes (OpenAI-compatible) cannot run from the console. | A transport in the profile (D12 step 3) and in the launch form |
| I20 | High | The suite editor cannot select scenarios in subfolders | It builds `scenarios/<id>*.yaml`, but the console itself saves new scenarios under `scenarios/<family>/`, and the scaffolds keep LLM scenarios in `scenarios/llm/`; the prefix also matches `rf-10` for `rf-1`. | Select by each scenario's exact file path, and show the suite's own patterns |
| I21 | High | A run with an agent failure can be headlined "not judged" in the console | When the gate blocked on an outage and a scenario also failed, the pill reads *not judged* while CI exits 1; the console never shows the exit code. | Never *not judged* when a scenario failed; show the exit code on the run |
| I22 | High | The pytest plugin ignores the release gate | `pytest-opseval` maps each scenario on its own: a run that judged nothing (no simulator key on a fork PR) is skipped and pytest exits 0 where `opseval run` exits 2; unmeasured, degraded and flaky checks are never named, and a harness error and an agent failure both exit 1. | Set pytest's exit status from the gate, print the gate summary, and document that pytest's exit codes are not 0/1/2/3 |
| I23 | High | Windows: every run in CI exits 3, and the Action cannot install | Output written to a pipe uses the Windows code page, so the first non-ASCII character in the console summary crashes the run (exit 3). `action.yml` uses `bin/` paths a Windows venv does not have. No CI job runs on Windows or macOS. | UTF-8-safe output at the CLI entry point, OS-aware paths in the Action, and a Windows and macOS smoke job (the four demo exit codes) |
| I24 | High | The going-public checklist is not met | DCO sign-off is required by CONTRIBUTING but not enforced (4 of 129 commits signed off); Discussions are disabled though five files send people there; private vulnerability reporting is off and the security and conduct addresses are unverified; the wheel, sdist and container image ship without LICENSE and NOTICE; the untracked `docs/Design system.pdf` is copied into a local docs build; one personal CODEOWNER; 60 remote branches would become public. | One PR for what is in the repo (licence files in every package, the PDF in `exclude_docs`, stale text), and a settings list for the owner (DCO check, Discussions, private reporting, CODEOWNERS team, branch clean-up); branch protection: GitHub refuses rulesets on this private repository, so `main` is protected softly now (a DCO check, a pre-push hook) and the committed rulesets are applied on the day it goes public (ADR-0065) |
| I25 | High (security) | An agent could influence its own grading | Some agent-supplied text reaches the judges outside the transcript fence (ADR-0036), the security judges included. Detail in the review (section 10). | Judge facts and context built only from the scenario, the profile and fixed values, with a test that holds every fact to that (D12 step 2b) |
| I26 | High (security) | Malformed agent output can stop a run or stall the harness | Certain replies end the run without a record, or hold the harness up long enough that its timeouts cannot fire; the agent is never failed for it. Detail in the review (section 10). | Limits on what a reply may be, where an over-limit reply is the agent's protocol fault, and a minimal record whenever a full one cannot be written (D12 step 2b) |
| I27 | High | "Safe to release" can be shown under a policy that ignores unmeasured checks or flakiness | With `unmeasured: ignore`, or under `lenient` with a flaky blocker, the summary reads *Safe to release*, which ADR-0030 and ADR-0032 rule out. | `safe` computed from the facts (everything measured, nothing flaky, every scenario passed), whatever the policy |
| I28 | High | Redacting credential-like key names changes checkpoint evidence | A payload key whose name contains a credential-like word (`signature_required`, `tokens_used`) is replaced before scoring, so `payload_equals` can fail a correct agent and `required_fields` can pass one that left a field empty; the change is not recorded in `redaction`. | Redact values, not keys; record what was redacted; a scorer that reads a redacted field says so (not measured) |
| I29 | High (security) | A credential in the agent's URL is written to reports | A token in `--target` (a `?token=` query) lands in the manifest, the result, the record, `report.html`, JUnit and the webhook payload. | Store and show the target without query and user info, as the tenant pool already does; redact the tokens Opseval itself holds by their value |
| I30 | High (security) | A package name the docs tell people to install is not registered | Anyone can claim it on PyPI, and every reader who follows the README would install their code once the repository is public. | Reserve every name the docs install, with D4, before going public |

I9 (no concurrency) closed with the test-tenant pool (ADR-0041). I10 (a silent agent passed), I11 (a tool run on the agent's server counted as never called), I12 (a
notification ended the turn early) and I15 (params and `kind: refusal` unchecked) closed with
ADR-0043.

## For your information

- **Behaviour you may notice (ADR-0043):** `scorers: default` now follows the profile (no
  `flow.phase_order` without phases; a declared `must_call` or `ordering` is checked), and every
  scaffolded scenario has `conversation.agent_replied`, which fails a turn answered with nothing.
  A misspelt or invalid scorer `params` now fails `opseval validate`. Leaving `stop_at` out on a
  profile without a commit gate means running to the goal. A tool with `commits: true` now needs a
  `commit_gate`, and without traces a `must_call` tool missing from the frames is *not measured*
  unless the profile sets `tool_calls_complete: true`.
- **The whole review is in `docs-internal/`** (`review-2026-10-20-any-team-adoption.md`): what
  already works for any team, the gaps behind I10–I16, and edits proposed to the planning
  documents, and the reviews of the console and its API (I17–I21), CI, packaging and
  integrations (I22–I24) and security since 2026-10-05 (I25–I29), with their medium and low
  findings. The 2026-10-05 review's core-rule findings R1–R6 are all fixed.
- **Behaviour you may notice:** every attempt now has a `protocol.replies_read` score. When the
  profile reads none of the agent's replies the attempt is not judged, and a run where every
  attempt is unread exits 3, not 0 (ADR-0042). HTTP-family turns now honour the scenario's
  `turn_timeout_seconds`, where they were always limited to 120 s.
- **Behaviour you may notice:** under the `standard` and `strict` presets a flaky pass, a skip or
  an unmeasured check reads "No blocking issues, but …" (yellow), never "Safe to release"
  (ADR-0030, ADR-0032). A policy that *ignores* unmeasured checks or flakiness can still say
  "Safe to release" today; that is I27.
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
- **The issue sinks have only met fakes:** the GitHub (ADR-0040) and Jira (ADR-0045) sinks are
  tested against mock APIs. Before relying on them, point each at a scratch repository or
  project once. On Jira, check that the create screen allows labels.
- **The console is not in the Python package yet**; that is a release-pipeline step for after
  Gate 0 (ADR-0034). It needs the Node versions in `packages/console/package.json` `engines`.
