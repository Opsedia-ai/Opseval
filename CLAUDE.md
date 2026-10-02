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
  driver.py      one attempt as a state machine; records, never judges
  simulator/     scripted + LLM personas, language packs (en, ar MSA/Egyptian/Gulf), typing noise
  llm.py         OpenAI-compatible client (no vendor SDKs, ADR-0003), retries, usage meter
  traces/        TraceSource + Langfuse v2 observations reader (ADR-0011)
  scoring/       base.py (registry/engine), deterministic.py, tools.py (trace-backed), judges.py (quoted ballots, N-vote judges: goal, language, persona [harness axis], security)
  verdicts.py    attempt verdicts, folding, release gate, exit codes
  runner.py      the launch pipeline used by the CLI (saves a scenario/profile snapshot per run)
  rescore.py     `opseval rescore`: offline re-scoring of saved records (ADR-0013)
  reporting/     self-contained HTML report, console summary, JUnit XML, Markdown gate summary
  scaffold/      `opseval init` templates
  demo/          reference refund agent + example catalogue (`opseval demo`)
  profiles/      built-in SUT profiles (openai-chat)
packages/opseval/tests/   pytest (asyncio_mode=auto), incl. the fault-injection suite
packages/pytest-opseval/  pytest plugin: scenarios as pytest items (entry point disabled in our own run)
action.yml                composite GitHub Action (dogfooded by the `action` CI job)
docs/adr/                 architecture decisions (0001-0017); new decisions get a new ADR
```

## Commands

```bash
uv sync                      # install (Python 3.11+)
make check                   # ruff, import-linter, bandit, mypy --strict, pytest (coverage >= 85%)
make audit                   # network: pip-audit + licence allowlist on runtime deps from uv.lock
uv run opseval demo          # healthy agent → exit 0
uv run opseval demo --bug skip-approval   # → exit 1   (also: forget-method → 1, crash → 3)
uv run opseval init /tmp/x && uv run opseval validate /tmp/x/suites/full.yaml --profile /tmp/x/profile.yaml
```

Definition of done for any change: `make check` is green, coverage stays ≥ 85%, and the four demo exit codes are unchanged. New behaviour comes with tests, plus docs (README, ROADMAP or ADR) when a user would notice it.

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
- v0.2 started: Langfuse trace reader and trace-backed tool scorers (`tools.expected_called`, `tools.forbidden_not_called`, `tools.ordering`, `tools.call_success`). The reader is verified against a real self-hosted Langfuse 4.44 (`scripts/langfuse_smoke.py`, ADR-0011); not yet against Langfuse Cloud.
- CI: `.github/workflows/ci.yml` runs lint, import-linter contracts (core never imports `ee/`; models are a leaf; scorers never touch transports), bandit, `mypy --strict`, pytest (`--cov-fail-under=85`) and the demo exit-code checks (0/1/3) on Python 3.11–3.13, plus an `audit` job (pip-audit, licence allowlist). `release.yml` publishes on `v*` tags (do not tag until Gate 0 is closed). `uv.lock` is committed; run `uv lock` after changing dependencies.
- PyPI: the name `opseval` is reserved (0.0.1 placeholder). Real releases should publish from `release.yml` via a PyPI Trusted Publisher (environment `pypi`).

## Next up (suggested order)

1. Opt-out, content-free telemetry (ADR-0010).
2. `opseval calibrate` (judge ↔ human agreement): measure judge bias instead of inferring it
   from the vendor name (ADR-0016).
3. SSE transport; OpenTelemetry trace source.
4. Adaptive, multi-turn attacks (PyRIT recipe) on top of planted injections.

## Planning documents

The product and business documents are not in this repo. They cover vision, business model, PRD, architecture, licensing/IP, governance, go-to-market, roadmap, KPIs and the risk register. They live in the Opsedia Google Drive folder. For local context, keep a copy under `docs-internal/`, which is gitignored, and read it when a task needs product context. Treat `docs-internal/06-*` (licensing and IP) as confidential.
