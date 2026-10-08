# ADR-0029: The console API, and what the browser is not allowed to do

- **Status:** Proposed — security and run outcomes revised by [ADR-0031](0031-securing-the-console-api.md)
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
implement, so the form now offers only `repeats`, `--fail-fast` and the commit guard, which it
does. A disabled-looking checkbox that silently does nothing is worse than its absence.

### The writes are four, and each fails closed

- `POST /runs` starts a run in the background and returns 202. The committed-outcome guard
  (ADR-0008) is untouched: `allow_commit` still needs the scenario's own flag, and the API cannot
  widen it. An unknown suite or missing target is a 4xx with a machine `code`, never a started run.
- `POST /simulator/rehearse` plays **one** persona turn against a transcript the human typed. It
  contacts no agent and uses no test account — the point is to catch a persona drifting out of
  character before an hour of real runs is spent on it. A scripted persona needs no model at all;
  an LLM persona with no simulator configured is a 503 naming the two variables to set, which is a
  configuration answer, not a failure.
- `PUT /suites` writes the YAML and then loads it back through `load_catalogue`, so a suite that
  resolves to nothing is reported before anyone runs it.
- `POST /scenarios/import` validates **every** file before writing **any**. One bad file leaves the
  catalogue byte-identical, and a file whose id already exists is refused rather than silently
  overwriting work. Both are tested by asserting the directory listing is unchanged after a
  rejected batch.

### Plain React Router, hand-mirrored types

TanStack Query/Router and shadcn/ui are not in; `react-router-dom` and about 200 lines of Tailwind
components are. For eleven mostly-read screens the heavier stack would have added dependencies
whose licences `make audit` does not yet cover, for caching behaviour this does not need.

`src/lib/types.ts` is written by hand against `schemas.py` rather than generated. That is a real
cost — the two can drift and nothing catches it — accepted because the generator is a build step
with no home yet (`make schemas` covers the *file* schemas, not the API). Generating the client
from the OpenAPI document FastAPI already serves is the obvious follow-up.

### The API is loopback-only because it has no authentication

`--host` defaults to `127.0.0.1`, and `GET /config` returns the same `config_digest()` that stamps
every run, so secrets are redacted server-side before serialisation rather than hidden by the UI.
That is the only protection there is. Binding this to an interface would publish an unauthenticated
endpoint that can start runs against a configured agent. Accounts and API keys are v0.5 and this
ADR does not pre-empt them.

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
