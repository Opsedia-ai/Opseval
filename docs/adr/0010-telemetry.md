# ADR-0010: Opt-out, content-free, documented telemetry

- **Status:** Proposed (detailed design awaiting approval; nothing is implemented)
- **Date:** 2026-09-22 (design detail added 2026-09-24)
- **Deciders:** Opseval maintainers

## Context

Usage data guides the roadmap and the open-core business (the north-star metric is Weekly Judged Attempts), but trust matters more. Teams run Opseval against pre-release agents, often in regulated environments, so telemetry must never carry conversation content, and must be easy to see and to turn off.

## Decision

Opseval sends anonymous, aggregated counts: a random install id, version, OS/arch, mode, and bucketed counts of runs, attempts, judged attempts, scenarios, users and projects, plus the transports and scorer ids in use. It **never** sends conversation text, scenarios, prompts, hosts, keys or personal data. Telemetry is disabled with `OPSEVAL_TELEMETRY=false` and off by default in CI (`CI=true`). `opseval telemetry show` prints the exact payload.

## Detailed design (for approval)

### When something is sent

One event per completed `opseval run` / `opseval demo` / `opseval rescore`, sent **after** the result is written. It is fire-and-forget: one HTTPS POST with a 2-second timeout, no retries, no queue on disk. It never changes the exit code, and it never delays the run by more than the timeout. Nothing is sent from `validate`, `init`, `summary` or `list-scorers`.

### Payload `opseval.telemetry/v1` (complete; nothing else is sent)

```jsonc
{
  "schema": "opseval.telemetry/v1",
  "install_id": "5f0c…",              // random UUID4, created on first run
  "event": "run_completed",           // run_completed | demo_completed | rescore_completed
  "opseval_version": "0.2.0",
  "python": "3.12",
  "os": "Linux", "arch": "x86_64",
  "mode": "local",                    // local | server (later)
  "ci": false,                        // true only when explicitly enabled in CI
  "scenarios": "6-20",                // buckets: 0, 1, 2-5, 6-20, 21-100, 100+
  "attempts": "21-100",
  "judged_attempts": "21-100",        // attempts with at least one agent-facing score
  "exit_code": 1,
  "gate": "block",
  "transports": ["websocket"],        // built-in names only; anything else -> "custom"
  "scorers": ["flow.phase_order", "judge.goal_achievement"],  // built-in ids only; "custom" otherwise
  "trace_source": "langfuse",         // built-in name or "none"
  "languages": ["ar", "en"],          // persona language codes only (no dialect, no text)
  "demo_target": true                 // the run targeted the bundled demo agent
}
```

**Never sent:** target URLs or hosts, profile names, scenario ids, names or content, transcripts, prompts, judge ballots, error messages, file paths, usernames, hostnames, API keys, tokens, or anything the user typed. The collector must not store IP addresses.

### Turning it off (any one is enough)

- `OPSEVAL_TELEMETRY=false`, or the community-standard `DO_NOT_TRACK=1`.
- Off automatically when `CI=true` (GitHub Actions, GitLab, etc.) unless `OPSEVAL_TELEMETRY=true` is set explicitly.
- `opseval telemetry disable` writes `telemetry: false` to the user config file (`$XDG_CONFIG_HOME/opseval/config.json`, `%APPDATA%\opseval\config.json` on Windows). `opseval telemetry enable` reverses it.

The first run that would send prints a one-time notice (stderr) saying what is sent, where, and how to turn it off. No event is sent on that first run.

### Seeing it

- `opseval telemetry show` prints the status (enabled, or disabled and why), the endpoint, the install id, and the exact JSON that the last run produced or would have produced.
- `opseval telemetry show --sample` prints a sample payload without running anything.
- The payload schema is versioned; a snapshot test fails if a field is added without a schema bump and a docs update.

### Implementation notes

- A `telemetry` module that depends only on `models` (an import-linter contract); the payload is built from `SuiteResult`, never from records.
- Settings: `OPSEVAL_TELEMETRY` (`auto` | `true` | `false`) and `OPSEVAL_TELEMETRY_ENDPOINT` (for self-hosted collectors, tests and air-gapped mirrors).
- Tests: payload snapshot; "never sent" fields asserted absent for a run whose scenario text, host and profile contain sentinel strings; CI and DO_NOT_TRACK switches; a network failure never changes the exit code.

## Decisions needed before implementation

1. **Allowlist vs opt-out.** The project rule is that every host that receives data must be in `OPSEVAL_ALLOWED_HOSTS`. Applied to telemetry, that makes it effectively **opt-in** (nobody allowlists a telemetry host by accident).
   - *Option A (recommended while the repo is private):* the telemetry host must be allowlisted. Telemetry is off in practice until design partners opt in. Revisit at public launch.
   - *Option B:* a documented exception for the fixed telemetry endpoint only. It receives no conversations or keys, so the allowlist's purpose (protecting credentials and conversations) is not at stake. This gives true opt-out.
2. **Endpoint and operator.** There is no Opsedia collector yet. The options are a small self-run endpoint (e.g. `telemetry.opsedia.ai`, EU-hosted) or a privacy-focused analytics provider with a DPA. The data-processing terms must name it.
3. **Timing.** Ship dark (code present, endpoint unset, so nothing is sent) now, and enable it only after the public release and the privacy notice. Or wait entirely until public launch.
4. **`languages` and `demo_target`.** These fields serve the activation and language-coverage metrics. Keep them, or drop them for an even smaller payload?

## Consequences

- Public metrics are reported as "among reporting installs".
- The payload schema is versioned and documented, and any change goes through this ADR.
- With Option A, the numbers will be small and biased toward design partners until launch.
