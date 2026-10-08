# ADR-0029: The console API, and what the browser is not allowed to do

- **Status:** Proposed
- **Date:** 2026-10-08
- **Deciders:** Opseval maintainers
- **Revises:** ADR-0004 (library choices and the dev-time origin; its contract still holds)

## Context

ADR-0004 chose React + TypeScript + Vite for the console and set the rule that matters: *the
browser never recomputes aggregates; it only renders server metrics.* It assumed TanStack
Query/Router, shadcn/ui, and an API client generated from the server's OpenAPI schema — but no
server existed to generate from, so none of that could be settled until one did.

v0.5 wants the full shared service (Postgres, S3, workers, accounts). That is a long way off, and
in the meantime a run is a folder on disk that only the CLI can read. Everything the console shows
— runs, verdicts, scores, the catalogue, the personas — already exists in `reports/` and the
scenario YAML. The gap was an HTTP surface over it, not new evaluation logic.

## Decision

`opseval serve` (the `serve` extra: FastAPI + uvicorn) over the existing `FileStore` and
`load_catalogue`. `opseval.server` holds four modules: `schemas` (wire shapes), `views` (pure
reads), `actions` (the four writes), and `app` (routing). No evaluation logic lives there — the
API calls `launch()` the same way the CLI does.

### A wire `null` is "not measured", and the browser renders it as such

The three-state contract (ADR-0006) only survives if it survives serialisation. `costUsd`,
`passRate`, `score` and the token counts are `float | None`, and a run that could not be judged is
a **gap** in the trend, not a zero. The console has one `<NotMeasured>` component and no code path
that coerces `null` to `0`, because a 0 on a chart reads as "the agent failed" — the same reason
sinks push an unmeasured verdict as a category (ADR-0027).

`GET /compare` separates **regressions** from **not judged** for the same reason. A scenario that
went `passed → inconclusive` is an outage, and listing it as a regression sends someone hunting a
bug that was really a dead credential.

### The browser computes nothing, and this had to be enforced

The first console drew its own headline tiles — "39% of runs cleared the gate", a flaky count — by
reducing over the runs list, and inferred run readiness from the *names* of config keys. Both
looked fine and both were the thing ADR-0004 forbids: a number on screen that no test covers and
the server cannot reproduce. Those tiles are gone, and readiness is now `GET /readiness`, which
answers what a run *could not* do and what to set to fix it.

The rule generalises: a React component is a bad place for an invariant, because the only thing
asserting it is whoever reads the diff. Anything with a contract belongs behind a Python test.

### No UI without an endpoint

Every control in the console maps to a route. The corollary is that controls whose backend does
not exist were **deleted**, not stubbed: the run form had stop conditions ("abandon after N
consecutive failures", on rate limit, on first critical failure) that the runner does not
implement, so the form now offers only `repeats` and `--fail-fast`, which it does. A
disabled-looking checkbox that silently does nothing is worse than its absence.

### The gate is the exit code, and a harness failure keeps its own colour

`_gate` maps 0/1/2/3 to `passed`/`failed`/`not_judged`/`harness_error`, and the console gives each
its own treatment. This is a correction. The first version mapped **exit 1 to "judged"**, which the
console painted in the pass colour, and **exit 3 to "blocked"**, which it painted in the same red
as a failing agent. That is the project's one rule inverted, in the one place a person actually
looks: a failing run read as green, and our own outage read as the agent's fault. `harness_error`
is deliberately not red.

### The writes are four, and each fails closed

- `POST /runs` starts a run in the background and returns 202. **`allow_commit` is the server's,
  not the request's** — `opseval serve --allow-commit`. ADR-0008 makes the irreversible outcome
  need two keys, the scenario's flag and the operator's; an earlier version of this took the
  second from the request body, which left the guard holding one key and a caller holding the
  other. An unknown suite or missing target is a 4xx with a machine `code`, never a started run.
- `POST /simulator/rehearse` plays **one** persona turn against a transcript the human typed. It
  contacts no agent and uses no test account — the point is to catch a persona drifting out of
  character before an hour of real runs is spent on it. A scripted persona needs no model at all;
  an LLM persona with no simulator configured is a 503 naming the two variables to set, which is a
  configuration answer, not a failure.
- `PUT /suites` resolves the suite against the catalogue in a scratch file **before** the real one
  is in place, refuses to replace an existing suite unless `overwrite` says so, and rejects a
  suite whose patterns match nothing — that one would run, pass, and report a clean gate on having
  tested nothing, which is the failure ADR-0020 refuses for preflight arriving by another door.
- `POST /scenarios/import` validates **every** file before writing **any**. One bad file leaves the
  catalogue byte-identical, and a file whose id already exists is refused rather than silently
  overwriting work. Both are tested by asserting the directory listing is unchanged after a
  rejected batch.

Neither write touches a catalogue inside the installed package. `opseval serve` with no
`--catalogue` serves the bundled demo so there is something to look at; editing it would mean
editing site-packages, so writes return 409 and the console disables the controls and says why.

### A 202 is a promise, so a launch that dies has to be reported

`POST /runs` answers before the run exists, which leaves two failures with nowhere to go: `launch()`
raising before it writes a manifest (no run id was ever assigned, so nothing on disk represents it)
and raising after (a folder with no `result.json`, which the runs list read as "still running" —
for ever). The first version caught both and did nothing with them, under a comment claiming they
surfaced. They did not.

`LaunchBook` records each launch; `RunOptions.on_start` hands back the run id the moment the
manifest exists, so a later crash can be attributed to that run. Both failures now appear in
`GET /runs` as `state: "error"`, `gate: "harness_error"`, carrying the reason. It is in-memory and
a restart loses it, which is honest for a single-process local server.

### An ad-hoc selection does not write to the catalogue

Running a hand-picked subset used to generate a temporary suite file inside `<catalogue>/suites/`.
A run that mutates the thing it is measuring is wrong on its own, and it also made selection
impossible on a read-only catalogue. `id=` filters now accept a comma-separated list, so the
subset is expressed as a filter and nothing is written. `RunOptions.suite_label` keeps such a run
from being labelled after whatever directory it was loaded from, which would group it with
unrelated runs in Compare and Trend.

### Plain React Router, hand-mirrored types

TanStack Query/Router and shadcn/ui are not in; `react-router-dom` and about 200 lines of Tailwind
components are. For eleven mostly-read screens the heavier stack would have added dependencies
whose licences `make audit` does not yet cover, for caching behaviour this does not need.

`src/lib/types.ts` is written by hand against `schemas.py` rather than generated. That is a real
cost — the two can drift and nothing catches it — accepted because the generator is a build step
with no home yet (`make schemas` covers the *file* schemas, not the API). Generating the client
from the OpenAPI document FastAPI already serves is the obvious follow-up.

### Binding to loopback is not a security control

The first version relied on `--host 127.0.0.1` and said so in the README. That is not enough, and
the gap is worth writing down because it is a common one.

**DNS rebinding.** A page on `evil.com` whose domain re-resolves to `127.0.0.1` is *same-origin
with itself*, so no CORS check ever runs, and the request arrives here carrying `Host: evil.com`.
Nothing about the operator's firewall or their laptop being "local" prevents it. The control is a
Host-header allowlist (`TrustedHostMiddleware`), which refuses anything that is not loopback. CORS
still has a job — it refuses an ordinary cross-origin JSON request — but it is the second lock,
not the first.

**Off loopback.** `--host` on a real interface is refused outright unless `--token` is given, with
an error rather than a warning, because this API can start runs against the configured agent and
spend the operator's LLM credit. With a token, the legitimate Host values cannot be derived (a
wildcard bind answers to every name the machine has), so the token becomes the control and the
Host check steps aside rather than guessing.

`GET /config` returns the same `config_digest()` that stamps every run, so secrets are redacted
server-side before serialisation rather than hidden by the UI. Request bodies are `extra="forbid"`
and bounded, so an ignored field is an error rather than a caller believing they asked for
something; `repeats` is bounded at the edge because the model's own `le=20` is skipped by the
`model_copy` the runner uses. Accounts and API keys are still v0.5 and this does not pre-empt them.

### Same-origin, eventually

ADR-0004 says the console ships as static files served by the API. That is still the plan and is
not built; today Vite proxies `/api` to `:8000`, and CORS allows only the two dev origins.

## Consequences

- A run folder is readable without the CLI, so the console, and later any client, sees exactly
  what `result.json` recorded.
- The console is honest about its own gaps: tracing off, no judge model, an unpriced model and a
  catalogue that failed to load each say so on screen, because `/readiness` and the load problems
  are served rather than inferred.
- **Three screens people will expect are not built, because their endpoints are not.** Run detail
  shows scores but no transcript (records are on disk and nothing serves them); a running run shows
  only "in progress", since the runner reports no per-scenario progress; and there is no scenario
  editor, only bulk import. Each needs backend work first, by the rule above.
- `uv.lock` now carries FastAPI, uvicorn and starlette in the dev group. They are an optional extra
  for installers, so `pip install opseval` is unchanged, but `make audit` now covers them.
- CI builds the console (`make console-check`), so a type error in the front end fails the build.
  It does not yet run the console against a live API.
- The hand-mirrored types are the weak seam. A field renamed in `schemas.py` typechecks fine on
  both sides and breaks at runtime.
- `RunOptions` gained `on_start` and `suite_label`, and the `id=` filter now accepts a list. All
  three are additive and the CLI is unchanged, but they are runner surface that exists for the
  API's benefit.
- **The launch book is in-memory.** Restart the server and the record of a launch that died is
  gone, leaving the orphaned folder reading as "running" again. Persisting it means writing a
  terminal marker into the run folder, which is the right fix when runs outlive one process.
- Much of what the review found was invisible to the test suite and to CI, which were green
  throughout. Three of the defects were claims in prose — a commit message and this ADR — that
  the code did not support. Tests now cover each one: a foreign `Host`, a body-supplied
  `allow_commit`, a launch that dies, the gate-to-colour mapping, a read-only catalogue, and a
  save that would have replaced a working suite.
