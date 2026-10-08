# CLAUDE.md: working on Opseval

Read this first in every session. It is the shared context for anyone (human or agent) contributing to this repo.

## What Opseval is

Open-source, black-box evaluation for conversational AI agents. An LLM (or a script) plays the customer. The conversation runs over the agent's real protocol. The simulated customer answers approval checkpoints. Every attempt is recorded, then scored by deterministic checks and quote-backed LLM judges. Repeated attempts fold into verdicts, then a release gate and CI exit codes.

Business model: open core, like Langfuse. The Apache-2.0 core lives here. Commercial Enterprise features go under `ee/` behind a licence key, and there will be a managed Cloud edition. The pledge in `GOVERNANCE.md` holds: evaluation features never move behind a licence key.

## The one rule that shapes every change

> A harness problem must never look like an agent failure, and a real agent failure must never be hidden as "not measured".

- Scores are `passed | failed | infra_error`. `score is None` exactly when the status is `infra_error` (validated in `ScoreResult`).
- Verdicts are `passed | failed | inconclusive | skipped_precondition | blocked`.
- Exit codes: `0` pass · `1` fail · `2` not judged · `3` harness/config error.
- Anything under `HarnessError` (transport, simulator, LLM, trace source) is never the agent's fault. A `TransportTimeout` *is* the agent's fault.
- An unmeasured value renders as "not measured", never as 0.
- Committed-outcome guard (ADR-0008): the default `stop_at` is `commit_gate`. The irreversible outcome needs `allow_commit: true` in the scenario **and** `--allow-commit` on the run.

When you add a transport, scorer or trace source, add fault-injection tests that prove both halves of the rule (see `tests/test_driver_and_faults.py`).

## Repository map

```
packages/opseval/src/opseval/
  models/        scenario.py (YAML schema, opseval/v1), profile.py (SUT profile), record.py (RunRecord, ToolCall), results.py
  catalogue.py   load + cross-check scenarios/suites/profiles (fail closed); builtin:<name> profiles
  auth.py        static and short-lived HS256 tokens (per attempt, refreshed before expiry)
  transport/     websocket.py (resume, token refresh), queued.py (http-json, python), openai_chat.py
  driver.py      one attempt as a state machine (CONNECT→PROBES→OPENING→…); records, never judges
  simulator/     scripted + LLM personas, language packs (en, ar MSA/Egyptian/Gulf), typing noise
  llm.py         OpenAI-compatible client (no vendor SDKs, ADR-0003), retries, usage meter
  traces/        TraceSource + Langfuse v2 reader (ADR-0011) + OTLP file reader (ADR-0028)
  scoring/       base.py (registry/engine), deterministic.py, tools.py (trace-backed), judges.py (quoted ballots, N-vote judges: goal, language, persona [harness axis], security)
  verdicts.py    attempt verdicts, folding, release gate, exit codes
  runner.py      the launch pipeline used by the CLI (saves a scenario/profile snapshot per run)
  rescore.py     `opseval rescore`: offline re-scoring of saved records (ADR-0013)
  calibrate.py   `opseval calibrate`: judge agreement with human labels (ADR-0018)
  preflight.py   dependency health checks before a run (ADR-0020)
  telemetry.py   opt-out, content-free counts; no endpoint ships (ADR-0010/0021)
  flow.py        flow map: expected vs observed phase transitions (ADR-0023)
  plugins.py     `opseval.scorers` + `opseval.sinks` discovery, fail-closed (ADR-0022/0027)
  sinks/         publish a finished run: webhook, Langfuse scores (ADR-0027)
  testing.py     public contract test kit for plugin authors (ADR-0022)
  reporting/     self-contained HTML report, console summary, JUnit XML, Markdown gate summary
  server/        `opseval serve`: HTTP API for the console — schemas (wire), views (pure reads),
                 actions (the four writes), app (routes). No evaluation logic (ADR-0029)
  scaffold/      `opseval init` templates
  demo/          reference refund agent + example catalogue (`opseval demo`)
  profiles/      built-in SUT profiles (openai-chat)
packages/opseval/tests/   pytest (asyncio_mode=auto), incl. the fault-injection suite
schemas/v1/               published JSON Schemas, generated from the models (ADR-0026)
packages/pytest-opseval/  pytest plugin: scenarios as pytest items (entry point disabled in our own run)
packages/console/         React + Vite web console (ADR-0004/0029); `src/lib/types.ts` mirrors server/schemas.py
action.yml                composite GitHub Action (dogfooded by the `action` CI job)
docs/adr/                 architecture decisions (0001-0032); new decisions get a new ADR
```

## Commands

```bash
uv sync                      # install (Python 3.11+)
make check                   # ruff, import-linter, bandit, mypy --strict, pytest (coverage >= 85%)
make audit                   # network: pip-audit + licence allowlist on runtime deps from uv.lock
uv run opseval demo          # healthy agent → exit 0
uv run opseval demo --bug skip-approval   # → exit 1   (also: forget-method → 1, crash → 3)
uv run opseval init /tmp/x && uv run opseval validate /tmp/x/suites/full.yaml --profile /tmp/x/profile.yaml

uv run opseval serve --target ws://127.0.0.1:8765   # API on :8000 (loopback only, no auth yet)
make console                 # the web console on :5173, proxying /api to :8000 (needs Node 20+)
make console-check           # typecheck + build the console (runs in CI)
```

Definition of done for any change: `make check` is green (which now includes `opseval schemas --check`, so run `opseval schemas` after touching a model), coverage stays ≥ 85%, and the four demo exit codes are unchanged. New behaviour comes with tests, plus docs (README, ROADMAP or ADR) when a user would notice it. **Every PR that changes what Opseval can do updates `docs/STATUS.md`** (the row's status and evidence, and the "As of" line), then runs `uv run python scripts/project_status.py` to recompute the percentages; `make check` fails if they are stale.

## Conventions

- Python 3.11, `mypy --strict`, ruff (line length 100). Pydantic v2 models with `extra="forbid"`. Authoring mistakes must fail at load time.
- Every settings value goes through `config.Settings` (`OPSEVAL_*`). No other module reads the environment. Secrets are `SecretStr` and appear in `SECRET_FIELDS`, and `config_digest` redacts them.
- Any host that receives a conversation or a key must be in `OPSEVAL_ALLOWED_HOSTS` and use TLS unless it is loopback (`check_url_allowed`).
- Scorers never raise to the engine. The engine turns exceptions into `infra_error` anyway, but return `self.infra(...)` with a `REASON_CODE: explanation`.
- Tool scorers report `details.source` (`trace` or `frames`) and set `degraded` for frame evidence.
- Commits: Conventional Commits (`feat(scope): ...`), DCO sign-off for external contributors.
- Keep the repo **private** until Gate 0 (IP clearance) is signed off. Never copy code, prompts, scenario content or names from any employer's internal systems (clean-room policy).

## Current status (update this section when it changes)

- v0.1 core: done. CLI, transports (websocket with resume and HS256 token refresh (ADR-0012), http-json, openai-chat, python), simulator, scoring, verdicts, reports, demo, `init`, JUnit.
- v0.2: `opseval rescore` re-scores saved runs offline and reports scorer/prompt changes (ADR-0013). Bump a scorer's `version` when its logic changes.
- v0.2: judges `judge.language_consistency`, `judge.persona_consistency` (harness axis: invalidates, never fails the agent) and the 3-vote `security.no_injection_takeover`; scenarios can plant `conversation.injections` (ADR-0014).
- v0.2: CI integrations: `action.yml` (composite GitHub Action), `opseval summary`, and the `pytest-opseval` package (ADR-0015).
- v0.2: `security.policy_adherence` (needs `expectations.policy`; empty is a load error), `judge.content_quality`, `judge.efficiency`, and judge independence: a judge sharing a vendor family with the model it grades (the simulator on the HARNESS axis) is refused at **launch** with a `ConfigError` (exit 3), never as a per-score `infra_error` — an `infra_error` is dropped from `measured`, so refusing while scoring would pass the suite with the coverage gone. Declare `profile.sut_model`; `OPSEVAL_JUDGE_INDEPENDENCE=warn|off` (ADR-0016).
- v0.2: a degraded score is named in the gate summary, JUnit and console (never only in the HTML); every judge declares a scenario-derived `rubric()` whose digest `rescore` checks before reusing a score; multi-vote ballots are concurrent and rotate the criteria so they are distinct prompts (ADR-0017).
- v0.2: `opseval calibrate <run> --labels` measures a scorer against human labels and reports the false-pass rate separately, because aggregate agreement hides the dangerous direction. Offline and read-only; exit codes 0/1/2/3 as usual (ADR-0018).
- v0.2: `--transport sse` for agents that stream a turn as Server-Sent Events. A stalled stream is a `TransportTimeout` (the agent's fault); a dropped one is `TransportClosed` (not judged); an undecodable event stays `_raw` and is never given a kind, so garbage cannot look like a clean turn (ADR-0019).
- v0.2: preflight health checks run before the first frame. A **dependency** outage skips only the scenarios it silences (`skipped_precondition`); an **unreachable agent** raises `ConfigError` (exit 3) rather than skipping, because a skip is only a gate warning and would report "safe to release" on a suite that tested nothing. `opseval preflight` runs the checks alone; `--no-preflight` and `--verify-llm` on `run` (ADR-0020).
- v0.1: telemetry is implemented and **ships dark** — no default endpoint, so nothing is sent until an operator sets `OPSEVAL_TELEMETRY_ENDPOINT` *and* allowlists it. Payload is a `extra="forbid"` model, counts are bucketed, non-built-in names become `custom`, and an import contract plus a sentinel test keep conversation content out. Off via `OPSEVAL_TELEMETRY=false`, `DO_NOT_TRACK=1`, `CI=true` or `opseval telemetry disable` (ADR-0021). PR #12 is still the design record to review.
- v0.2: scorer plugins via the `opseval.scorers` entry-point group. Discovery **fails closed**: an unimportable plugin, a contract violation or an id collision is a `ConfigError` (exit 3), never a skip — a skipped scorer becomes `UNKNOWN_SCORER`, an `infra_error`, which `attempt_verdict` drops from `measured`, so the suite would go green with the check gone. `opseval.testing.ScorerContract` is the public kit; it claims only what `ScoreResult` does not already validate (ADR-0022).
- v0.2: flow map in the HTML report — per attempt, expected vs observed phase transitions, opening itself when the path was not clean. No phases or no markers is *not measured* with the reason, never an empty diagram. It changes no status, score or exit code; `flow.phase_order` still decides. `render_html` takes an optional profile for the declared order (ADR-0023).
- v0.2: `tools.args_valid` checks a call's arguments against the tool's `args_schema` (JSON Schema, needs full trace coverage; frames never carry arguments). No declared schema is `NOT_MEASURABLE`, a broken schema is reported as the *schema* being broken, and a called tool without one is `unchecked`, never failed. A **vacuous** pass (the situation never arose) is now named in the gate summary, JUnit, console and HTML — it was only in `details` before, so five scorers could pass on no evidence and look identical to a real pass. It changes no status or exit code (ADR-0024).
- v0.2: protocol probes (`profile.probes`) run in the driver's PROBES state, after connect and before the opening message, graded by `protocol.probes_passed`. They never abort the conversation. No answer is a failed probe (the agent's fault); a transport error is `transport_failure` (not judged); declared-but-never-run is `NOT_MEASURABLE`. An **error reply fails even an empty expectation** unless the probe declares `expect.kind: error` — found against the reference agent, which answers an unknown frame with `error: unknown_frame` (ADR-0025).
- v0.1/v0.2: JSON Schemas for scenario, suite, profile, record and result are generated from the models into `schemas/v1/` (FR-RPT-02, FR-CAT-11). The models are the source of truth and **drift is a failure**: `opseval schemas --check` runs in `make check` and CI, because a stale schema would have an editor accept a file the loader rejects. Generated `by_alias`, so `schema` not `schema_`. `opseval init` writes the `yaml-language-server` hint (ADR-0026).
- v0.5: sinks publish a finished run (`OPSEVAL_WEBHOOK_URL`, `OPSEVAL_LANGFUSE_PUSH_SCORES`). A sink can **never** change a verdict or exit code — every failure is caught — but it must **not** fail silently either: failures land in `result.sinks`, the console and the gate summary, because a sink was asked for. Destinations are allowlisted before the run (exit 3). An unmeasured verdict is pushed as a *category*, never as 0, since a 0 on a dashboard reads as an agent failure (ADR-0027).
- v0.5: `traces.source: otlp-file` reads the OTLP-JSON the OpenTelemetry Collector's file exporter writes (`OPSEVAL_OTLP_SPANS_PATH`), so tool calls can be verified without Langfuse (R-08). OTLP has no query API, so a backend fetcher is deliberately not built — the reusable part is the span→ToolCall mapping. An **empty list is a positive claim** ("the agent called no tools"), so unreadable spans raise `TraceUnavailable` rather than returning `[]`. Only OTLP status 2 (ERROR) is a failure; UNSET is not (ADR-0028).
- v0.5: `opseval serve` (the `serve` extra) exposes the run folders and the catalogue over HTTP, and `packages/console/` is the React console on top of it (ADR-0029). Two rules decide what goes in it: **the browser computes nothing** — an aggregate the server cannot reproduce is a number no test covers, so readiness became `GET /readiness` and the browser-side headline tiles were deleted — and **no UI without an endpoint**, so run-stop conditions the runner does not implement were removed from the form rather than stubbed. A wire `null` is "not measured" and renders as such, never as 0. Writes are four and fail closed: starting a run (the ADR-0008 commit guard untouched), one simulator turn (no agent, no test account), saving a suite, and an all-or-nothing import that never overwrites an existing id. The API is **loopback-only and unauthenticated**; `/config` serves the redacted `config_digest`. Not built: transcripts in run detail, per-scenario progress, a scenario editor.
- v0.5: the console API is secured (ADR-0031): bearer token on every `/api` request (`OPSEVAL_SERVE_TOKEN` or random per start, carried by the printed console link), loopback `Host` only (DNS rebinding), no cross-site writes, no CORS, `--host` loopback-only. Commit needs `serve --allow-commit` (never a request field). Runs run in their own thread with a pre-minted id; a failure writes `error.json` (a `harness_error` run, counts `null`), and a dead pid on this host is `INTERRUPTED`. Gate state per run (`safe | gaps | blocked | not_judged | harness_error`) is decided once in `views.gate_state`. Writes are atomic and never overwrite unasked; the demo catalogue is read-only.
- v0.2: gate policy per suite (ADR-0032): `gate:` in the suite YAML (`preset: strict|standard|lenient` + per-outcome `block|warn|ignore`), `--gate PRESET` on `run`/`rescore`. Facts stay fixed; policy is recorded on `Gate.policy` and named in every output, with anything looser than standard flagged. Never configurable: nothing judged blocks; exit 3 stays 3; a blocking gate never exits 0; "Safe to release" (`Gate.safe`) needs every scenario passed under any policy; `commit_guard` cannot block; `inconclusive`/`load_errors` cannot be ignored. `--fail-fast` stops only on a failure the policy blocks and records the rest as `skip_kind: fail_fast`. `TestVerdict.skip_kind` tells outages from the commit guard. `standard` = previous gate, except a flaky pass is now a gap.
- v0.2: four core-rule leaks closed (ADR-0030). A suite that judged nothing **blocks** the gate (skips stay warnings: `skipped_precondition` is both an outage and the commit guard, so blocking on it would push teams to `--allow-commit`). "Safe to release" only when `SuiteResult.safe_to_release` (gate passed, exit 0, every check measured); otherwise "No blocking issues, but not everything passed", yellow / `PASS WITH GAPS`. An agent-facing `infra_error` inside a judged attempt is named **not measured** everywhere (`unmeasured_scorers`). `httpx.ConnectTimeout`/`PoolTimeout` are harness errors (`transport.base.NOT_SENT_TIMEOUTS`, caught before `TimeoutException`; SSE keeps them shorter than its turn deadline). The console script is `opseval.cli:entrypoint`: an unhandled exception exits 3, never 1. Still open: simulator time counts against the agent's deadline; `committed_outcome` without `commit_gate` loads; Click's Ctrl-C exits 1.
- v0.2 started: Langfuse trace reader and trace-backed tool scorers (`tools.expected_called`, `tools.forbidden_not_called`, `tools.ordering`, `tools.call_success`). The reader is verified against a real self-hosted Langfuse 4.44 (`scripts/langfuse_smoke.py`, ADR-0011); not yet against Langfuse Cloud.
- CI: `.github/workflows/ci.yml` runs lint, import-linter contracts (core never imports `ee/`; models are a leaf; scorers never touch transports), bandit, `mypy --strict`, pytest (`--cov-fail-under=85`) and the demo exit-code checks (0/1/1/3) on Python 3.11–3.13, plus an `audit` job (pip-audit, licence allowlist) and a `console` job (`npm ci && npm run build`). `release.yml` publishes on `v*` tags (do not tag until Gate 0 is closed). `uv.lock` is committed; run `uv lock` after changing dependencies.
- PyPI: the name `opseval` is reserved (0.0.1 placeholder). Real releases should publish from `release.yml` via a PyPI Trusted Publisher (environment `pypi`).

## Next up (suggested order)

1. Issue-publisher sink (FR-INT-05); a Tempo/Jaeger fetcher reusing the OTLP mapping.
2. Adaptive, multi-turn attacks (PyRIT recipe) on top of planted injections.
3. Plugin groups beyond scorers: transports, langpacks, trace sources, sinks (ADR-0022).

**Needs a decision from the maintainers, not more code:** a telemetry collector and its
data-processing terms (ADR-0010 decisions 2 and 3). The code is built and dark until then.

## Planning documents

The product and business documents are not in this repo. They cover vision, business model, PRD, architecture, licensing/IP, governance, go-to-market, roadmap, KPIs and the risk register. They live in the Opsedia Google Drive folder. For local context, keep a copy under `docs-internal/`, which is gitignored, and read it when a task needs product context. Treat `docs-internal/06-*` (licensing and IP) as confidential.
