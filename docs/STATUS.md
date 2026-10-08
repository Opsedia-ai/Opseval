# Project status

Updated with every pull request that changes what Opseval can do (see CONTRIBUTING and
CLAUDE.md, "Definition of done"). Decisions waiting on the maintainers, and critical or high
issues, are in [`ATTENTION.md`](ATTENTION.md). The summary below is **computed** from the table by
`scripts/project_status.py`; `make check` and CI fail if it is out of date, so the headline
number cannot drift from the rows.

**As of 2026-10-08** (`main` with the egress, secrets and release-pipeline fixes).

## Completion

<!-- status:summary:start -->
**Overall: 79% of 88 functional requirements** (Done = 1, Partial = ½, Missing = 0). Must-haves: 82% of 69.

| Release | Requirements | Done / Partial / Missing | Complete |
|---|---|---|---|
| v0.1 | 59 | 44 / 12 / 3 | 85% |
| v0.2 | 15 | 12 / 2 / 1 | 87% |
| v0.5 | 12 | 5 / 3 / 4 | 54% |
| v0.8 | 1 | 0 / 0 / 1 | 0% |
| v1.x | 1 | 0 / 0 / 1 | 0% |
| **All** | 88 | 61 / 17 / 10 | **79%** |
<!-- status:summary:end -->

How to read it: one row per functional requirement in the product requirements (doc 04),
weighted equally. `Partial` means some of the requirement works and the rest is named in the
evidence column. A deliberate, recorded divergence from the requirement (an ADR) counts as
`Done` and says so.

## Not code: what gates the next milestones

Gate 0, the product name, design partners, calibration labels and the telemetry collector are
decisions, not code. They are not in the percentage, and today they gate more than code does.
They are tracked in one place: [`ATTENTION.md`](ATTENTION.md).

## Requirements

| ID | What | Pri | Rel | Status | Evidence |
|---|---|---|---|---|---|
| FR-CAT-01 | Offline `validate`, file + field + reason | M | v0.1 | Done | `catalogue.load_catalogue`, `LoadProblem` |
| FR-CAT-02 | Identity fields and criticality | M | v0.1 | Done | `models/scenario.py` |
| FR-CAT-03 | Persona fields, timezone checked | M | v0.1 | Done | `Persona`, `check_references` |
| FR-CAT-04 | Conversation spec | M | v0.1 | Done | Diverged by decision: `stop_at` defaults to `commit_gate` (ADR-0008); probes live in the profile (ADR-0025) |
| FR-CAT-05 | Expectations | M | v0.1 | Done | `Expectations`, `ExpectedTools` |
| FR-CAT-06 | Scorer list or default pack | M | v0.1 | Done | `ScorerSpec` |
| FR-CAT-07 | Execution: repeats, threshold, timeouts | M | v0.1 | Done | `ExecutionSpec` |
| FR-CAT-08 | Fail closed on authoring mistakes | M | v0.1 | Done | `extra="forbid"`, `check_references` |
| FR-CAT-09 | Suites and precedence | M | v0.1 | Done | `_load_scenario` deep merge |
| FR-CAT-10 | Filters and `blocked` | S | v0.1 | Done | `_match_filter`, `is_blocked` |
| FR-CAT-11 | Published JSON Schemas | S | v0.2 | Done | `schemas/v1`, `opseval schemas --check` (ADR-0026) |
| FR-CAT-12 | Scenario generation | C | v1.x | Missing | Not started (not due) |
| FR-SIM-01 | LLM persona, seeded, temperature 0 | M | v0.1 | Done | `LLMSimulator`, `attempt_seed` |
| FR-SIM-02 | Language packs (en, ar) as plugins | M | v0.1 | Partial | en, ar MSA/Egyptian/Gulf ship; not plugins; no code-switching; Arabic packs lack native review |
| FR-SIM-03 | Typing noise | M | v0.1 | Done | `simulator/typing_noise.py` |
| FR-SIM-04 | Planted injections | M | v0.2 | Done | `conversation.injections` (ADR-0014) |
| FR-SIM-05 | Checkpoint decisions | M | v0.1 | Done | `Simulator.decide` |
| FR-SIM-06 | Simulator fault is a harness error | M | v0.1 | Done | fault-injection suite |
| FR-SIM-07 | Persona playground | S | v0.5 | Done | console Simulator page, `POST /simulator/rehearse` |
| FR-RUN-01 | Pluggable transports | M | v0.1 | Done | websocket, http-json, sse, openai-chat, python (API only) |
| FR-RUN-02 | SUT profile | M | v0.1 | Done | `models/profile.py` |
| FR-RUN-03 | Credentials | M | v0.1 | Partial | static and HS256 with refresh (ADR-0012); no custom provider plugin |
| FR-RUN-04 | Driver state machine | M | v0.1 | Done | `driver.py`, PROBES state (ADR-0025) |
| FR-RUN-05 | Terminal states | M | v0.1 | Done | `TerminalState` |
| FR-RUN-06 | Timeouts count against the agent | M | v0.1 | Partial | connect/pool timeouts are harness errors (ADR-0030); time spent waiting on the simulator still counts against the agent's deadline (ATTENTION I1) |
| FR-RUN-07 | Protocol probes | S | v0.2 | Done | `profile.probes` (ADR-0025) |
| FR-RUN-08 | Tenant pool and locks | M | v0.1 | Missing | runner is sequential; a `feat/tenant-pool` branch exists, unmerged |
| FR-RUN-09 | Isolated attempts | M | v0.1 | Done | new driver, transport and simulator per attempt |
| FR-CHK-01 | Checkpoint policies | M | v0.1 | Done | `CheckpointPolicy` |
| FR-CHK-02 | Resume channel | M | v0.1 | Done | `driver._checkpoint` |
| FR-CHK-03 | Rejections | M | v0.1 | Done | rejection loop guard |
| FR-CHK-04 | Status-check checkpoints | S | v0.2 | Missing | not started |
| FR-CHK-05 | `stop_at` | M | v0.1 | Done | commit guard tests |
| FR-REC-01 | Run record | M | v0.1 | Partial | no "created artifacts" field |
| FR-REC-02 | Persist as you go | M | v0.1 | Done | `FileStore`, `result.json` last, `error.json` on failure (ADR-0031) |
| FR-REC-03 | Redaction | M | v0.1 | Partial | key names, plus credential *values* in text and payloads (ADR-0033); PII is deliberately kept, and unrecognised token formats pass through |
| FR-REC-04 | Trace enrichment | M | v0.2 | Partial | Langfuse and OTLP tool calls; no SUT scores or release tag |
| FR-REC-05 | Layout contract | M | v0.1 | Partial | documented in `storage.py`; no layout version; no `Storage` protocol (ADR-0005 is stale) |
| FR-SCR-01 | Three-state scores | M | v0.1 | Done | `ScoreResult` validator |
| FR-SCR-02 | Axes | M | v0.1 | Done | `Axis` |
| FR-SCR-03 | Deterministic scorers | M | v0.1 | Partial | `output.required_fields` is `output.payload_equals` |
| FR-SCR-04 | Trace-backed tool scorers | M | v0.2 | Partial | expected/forbidden/ordering/call_success/args_valid; no separate `approval_gated` or `flow.checkpoint_gating` |
| FR-SCR-05 | LLM judges with quoted ballots | M | v0.1 | Done | `scoring/judges.py` |
| FR-SCR-06 | Quality and security judges | M | v0.2 | Done | ADR-0014, ADR-0016 |
| FR-SCR-07 | Judge audit trail | M | v0.1 | Done | prompt hash, quotes, model; rubric digests (ADR-0017) |
| FR-SCR-08 | Vendor-family guard | S | v0.2 | Done | refused at launch by default (ADR-0016) |
| FR-SCR-09 | Criterion permutation | C | v0.5 | Done | rotated ballots (ADR-0017) |
| FR-SCR-10 | Scorer plugin API and contract kit | M | v0.2 | Done | `plugins.py`, `opseval.testing.ScorerContract` (ADR-0022) |
| FR-SCR-11 | Invariants and default pack | M | v0.1 | Partial | two invariants map to one scorer |
| FR-VER-01 | Attempt invalidation | M | v0.1 | Done | `attempt_verdict` |
| FR-VER-02 | Fold attempts | M | v0.1 | Done | `fold_attempts` |
| FR-VER-03 | Flaky detection | M | v0.1 | Done | `TestVerdict.flaky` |
| FR-VER-04 | Five-state verdicts | M | v0.1 | Done | `VerdictStatus` |
| FR-VER-05 | pass^k | S | v0.2 | Done | `_pass_hat_k` |
| FR-GATE-01 | Exit codes 0/1/2/3 | M | v0.1 | Done | `exit_code`, CI demo checks 0/1/1/3 |
| FR-GATE-02 | Criticality-aware gate | M | v0.1 | Done | `release_gate`; per-suite gate policy with presets, recorded in every result (ADR-0032); "Safe to release" only when nothing blocks or warns (ADR-0030) |
| FR-GATE-03 | GitHub Action | M | v0.2 | Done | `action.yml`, dogfooded in CI |
| FR-GATE-04 | pytest plugin | S | v0.2 | Done | `packages/pytest-opseval` |
| FR-GATE-05 | Other CI templates | S | v0.5 | Missing | only `opseval summary` |
| FR-RPT-01 | Offline, RTL-aware HTML report | M | v0.1 | Done | `report.html.j2`; no automated no-network or Arabic rendering test |
| FR-RPT-02 | `result.json` and its schema | M | v0.1 | Done | `schemas/v1/result.json` |
| FR-RPT-03 | Plain-language verdict sentence | M | v0.1 | Done | `Gate.summary` |
| FR-RPT-04 | Usage and cost, "no rate" not $0 | M | v0.1 | Done | report usage table |
| FR-RPT-05 | Flow map | S | v0.2 | Done | ADR-0023 |
| FR-RPT-06 | Compare and trend | M | v0.5 | Partial | compare and per-run trend in the console; no per-scenario trend |
| FR-RPT-07 | JUnit XML | S | v0.2 | Done | `reporting/junit.py` |
| FR-SAF-01 | Host allowlist, TLS | M | v0.1 | Done | targets, every LLM endpoint (keyed or not, preflight probes included), Langfuse, sinks, telemetry (`check_url_allowed`) |
| FR-SAF-02 | Secrets and redacted digest | M | v0.1 | Done | `SECRET_FIELDS` (webhook URL included), `config_digest`; sink outcomes name only the host |
| FR-SAF-03 | Test-tenant isolation and locks | M | v0.1 | Missing | not started (see FR-RUN-08) |
| FR-SAF-04 | Committed-outcome guard | M | v0.1 | Partial | CLI and console (`serve --allow-commit`, ADR-0031); a profile without a `commit_gate` is not yet refused (ATTENTION I7) |
| FR-SAF-05 | Artifact cleanup planning | S | v0.5 | Missing | not started |
| FR-SAF-06 | Retention | M | v0.5 | Missing | not started |
| FR-SAF-07 | Content-free telemetry | M | v0.1 | Done | implemented, ships dark (ADR-0021) |
| FR-INT-01 | OpenAI-compatible LLMs, no vendor SDKs | M | v0.1 | Done | `llm.py` (ADR-0003) |
| FR-INT-02 | Langfuse traces | M | v0.2 | Done | verified on self-hosted 4.44; not yet on Langfuse Cloud |
| FR-INT-03 | OTLP traces | S | v0.5 | Done | `traces.source: otlp-file` (ADR-0028) |
| FR-INT-04 | Score push to Langfuse | S | v0.5 | Done | sinks (ADR-0027) |
| FR-INT-05 | Issue publisher | C | v0.5 | Missing | not started |
| FR-INT-06 | PyRIT adapter | S | v0.8 | Missing | not started (not due) |
| FR-INT-07 | Webhooks | S | v0.5 | Done | sinks (ADR-0027) |
| FR-DX-01 | `pip install` and a container image | M | v0.1 | Partial | name reserved (0.0.1 placeholder); no release yet (Gate 0); no image |
| FR-DX-02 | Interactive `init` | M | v0.1 | Partial | template-based, not interactive |
| FR-DX-03 | Demo | M | v0.1 | Done | `opseval demo`, exit codes 0/1/1/3 |
| FR-DX-04 | Docs site | M | v0.1 | Missing | README and ADRs only |
| FR-DX-05 | Actionable errors | M | v0.1 | Done | `ConfigError` messages |
| FR-CTL | Run control: stop policies, cancel, rescore, calibrate, preflight | M | v0.1 | Partial | rescore (with `--gate` re-gating), calibrate and preflight done; only `fail_fast` of five stop policies; no cancel |
| FR-AUT | Authoring workbench | M | v0.5 | Partial | scenario YAML editor with the loader's checks as you type, saved only when valid and never over a newer file; bulk import; suite editor with the release-gate editor (ADR-0032); persona playground. Missing: a warnings / "not checked" tier beside errors |
| FR-SRV | Server mode | M | v0.5 | Partial | local `serve` API, serving the built console same-origin (ADR-0034) with run detail down to each attempt's conversation and live progress for runs in flight, token auth (ADR-0029, ADR-0031); no Postgres, workers, accounts, OIDC or live events; console not yet in the wheel |

## Non-functional requirements

| Area | Status | Evidence |
|---|---|---|
| Reliability | Done | fault-injection suite across transports, scorers and trace sources |
| Code quality | Done | `mypy --strict`, ruff, coverage ≥ 85% (≈ 95%), scorer contract kit; console API types generated from the server models with a drift check; console tests (Vitest) in CI; no transport contract kit |
| Determinism | Done | seeds, prompt and rubric hashes, `opseval rescore` |
| Security | Partial | bandit, pip-audit, SECURITY.md, console API token auth, actions pinned to SHAs with Dependabot, releases gated on `make check` and the demo exit codes; no threat model, SBOM or signed images |
| Privacy | Partial | content-free telemetry; credential values redacted (ADR-0033); console loads nothing from another origin (fonts bundled) |
| Scalability | Missing | attempts run one after another (≥ 20 concurrent is the target) |
| Performance | Missing | no overhead benchmark |
| Portability | Partial | CI on Linux only; Windows/macOS untested |
| Accessibility | Partial | console uses text with every colour; no WCAG audit |
| i18n | Partial | RTL report; UI strings not externalised |
