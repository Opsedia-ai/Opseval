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
  transport/     websocket.py, queued.py (http-json, python), openai_chat.py
  driver.py      one attempt as a state machine; records, never judges
  simulator/     scripted + LLM personas, language packs (en, ar MSA/Egyptian/Gulf), typing noise
  llm.py         OpenAI-compatible client (no vendor SDKs, ADR-0003), retries, usage meter
  traces/        TraceSource + Langfuse v2 observations reader (ADR-0011)
  scoring/       base.py (registry/engine), deterministic.py, tools.py (trace-backed), judges.py
  verdicts.py    attempt verdicts, folding, release gate, exit codes
  runner.py      the launch pipeline used by the CLI
  reporting/     self-contained HTML report, console summary, JUnit XML
  scaffold/      `opseval init` templates
  demo/          reference refund agent + example catalogue (`opseval demo`)
  profiles/      built-in SUT profiles (openai-chat)
packages/opseval/tests/   pytest (asyncio_mode=auto), incl. the fault-injection suite
docs/adr/                 architecture decisions (0001-0011); new decisions get a new ADR
```

## Commands

```bash
uv sync                      # install (Python 3.11+)
make check                   # ruff + ruff format --check + mypy --strict + pytest with coverage
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

- v0.1 core: done. CLI, transports (websocket, http-json, openai-chat, python), simulator, scoring, verdicts, reports, demo, `init`, JUnit.
- v0.2 started: Langfuse trace reader and trace-backed tool scorers (`tools.expected_called`, `tools.forbidden_not_called`, `tools.ordering`, `tools.call_success`).
- Not yet in the repo: `.github/workflows/ci.yml` and `release.yml` (the GitHub connector used so far could not push workflow files; add them by hand), and `uv.lock` (run `uv lock` and commit it).
- PyPI: the name `opseval` is reserved (0.0.1 placeholder). Real releases should publish from `release.yml` via a PyPI Trusted Publisher (environment `pypi`).

## Next up (suggested order)

1. WebSocket reconnect/resume and token refresh (HS256 short-lived tokens from `OPSEVAL_TOKEN_SIGNING_KEY`).
2. `opseval rescore <run>`: re-score saved records offline. The judge and scorer versions are already in the details.
3. More judges: language consistency, persona consistency (harness axis), 3-vote security judges with prompt-injection planting.
4. GitHub Action + `pytest-opseval` plugin.
5. Opt-out, content-free telemetry (ADR-0010).
6. SSE transport; OpenTelemetry trace source.

## Planning documents

The product and business documents are not in this repo. They cover vision, business model, PRD, architecture, licensing/IP, governance, go-to-market, roadmap, KPIs and the risk register. They live in the Opsedia Google Drive folder. For local context, keep a copy under `docs-internal/`, which is gitignored, and read it when a task needs product context. Treat `docs-internal/06-*` (licensing and IP) as confidential.
