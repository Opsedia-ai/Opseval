# ADR-0031: Securing the console API, and runs that always end visibly

- **Status:** Proposed
- **Date:** 2026-10-08
- **Deciders:** Opseval maintainers

## Context

ADR-0029 made `opseval serve` an HTTP API over the run folders and the catalogue, and
shipped it with no authentication, on the reasoning that it binds to loopback. A review of
the merged server found that loopback alone does not protect it.

- **Any web page could drive it.** With no `Host` check, a page that rebinds its own hostname
  to 127.0.0.1 (DNS rebinding) reached every route. A test client presenting
  `Host: attacker.example` imported scenarios, overwrote a suite, started runs and called the
  simulator. Those runs carry the operator's agent token and LLM keys.
- **The commit guard was one request.** ADR-0008 requires two keys for the irreversible
  outcome: `allow_commit: true` in the scenario and `--allow-commit` from the operator. The API
  took the second key from the request body, and the import route accepts scenarios that carry
  the first. `repeats` was unbounded and applied with `model_copy`, which skips validation:
  `repeats: -1` raised `ZeroDivisionError` partway through a run, and `500` ran 500 attempts.
- **A run could vanish or run forever.** A launch that failed (agent unreachable, a host not on
  the allowlist, the judge-independence refusal) was caught with `pass` while the API answered
  "run started". A run whose process died stayed "running" for ever. Both break the one rule:
  a harness problem became an absence.
- **The console drew exit 1 green.** Exit 0 and exit 1 both mapped to "judged", rendered as a
  pass, and exit 3 was red like a failure.
- **Writes were not all or nothing.** A suite was written before it was validated and silently
  replaced an existing one. Import checked ids but not file names, so a new id under an old file
  name deleted a scenario. Without `--catalogue` the server wrote into the demo catalogue inside
  the installed package.

## Decision

**Who may ask.** Every `/api` request must carry `Authorization: Bearer <token>`. The token is
`OPSEVAL_SERVE_TOKEN` when set (a `SecretStr`, redacted like every secret), otherwise random per
start. `opseval serve` prints a console link that carries it in the URL fragment, which no
server and no log ever sees. The console keeps it for the browser and drops it from the address
bar. In addition:

- Requests whose `Host` is not a loopback name (`127.0.0.1`, `localhost`, `::1`) get 403, even
  with the token: that is the DNS-rebinding defence.
- A write (`POST`, `PUT`, `PATCH`, `DELETE`) with a non-loopback `Origin` gets 403.
- There is no CORS. The console reaches the API same-origin, through the Vite dev proxy
  (`OPSEVAL_API` points it at another port).
- `opseval serve --host` refuses anything but loopback, with exit 3. Remote access goes through
  an SSH tunnel; a TLS and accounts story belongs to server mode, not this local tool.

**The commit guard over HTTP.** The operator's key is the start flag
`opseval serve --allow-commit`. A request asking for `allowCommit` without it gets 403
`COMMIT_NOT_ALLOWED`. The scenario's own `allow_commit: true` is still required, so all three
must agree: scenario, server flag and request. `/health` reports the flag, and the console
disables the switch when it is off.

**Request bodies are strict.** Request models forbid unknown fields, so a typo such as
`allowCommitt` is a 422 instead of being dropped. `repeats` is bounded to 1–20, as it is for a
scenario. Lists and texts have size limits. Response models stay lenient.

**Runs always end visibly.**

- The run id is minted before the run starts, and the API returns it.
- The run executes in its own thread and event loop, so the API keeps answering while it runs.
- If it fails before or during the run, `error.json` is written in the run folder in place of
  `result.json`: code, message and exit code 3. It is listed as `harness_error` with the reason,
  with counts of `null` (not measured), never 0.
- The manifest records the runner's pid and host. A run whose process no longer exists on this
  host is shown as `INTERRUPTED`. A run from another host, or an older manifest with no pid,
  stays "running", because guessing it dead could hide a run that is still going.
- A selection of scenarios is passed to the runner as `RunOptions.only_ids` instead of a
  temporary suite file in the user's catalogue. An unknown id is a configuration error, never
  silently dropped.

**One gate state, decided on the server.** `views.gate_state` maps a finished run to `safe`,
`gaps`, `blocked`, `not_judged` or `harness_error`, using `SuiteResult.safe_to_release` from
ADR-0030. Only `safe` is green, only an agent failure that blocks is red, and a harness error
is drawn as neither. Run detail also carries unmeasured, vacuous and degraded scorers, and sinks
that failed to publish, so the console names every gap the other outputs name.

**Writes are all or nothing.**

- A suite is validated under a dot-file name next to its target, then moved into place with
  `os.replace`. A suite that selects no scenario is refused (`EMPTY_SUITE`).
- Replacing an existing suite needs `overwrite: true`.
- Import refuses a file name that already exists, refuses an id given twice in one batch, and
  writes with `open("x")`. A write that fails halfway is rolled back.
- Without `--catalogue`, the demo is served read-only (409 `READ_ONLY_CATALOGUE`).
- Plugin scorers are loaded when the app starts, as `launch()` does, so a suite naming one is not
  rejected as `UNKNOWN_SCORER`.

## Consequences

- The console needs the token. Opening it without the link gives 401 and an error naming the
  fix. The token survives a page reload. It changes per start unless `OPSEVAL_SERVE_TOKEN` is
  set.
- Scripts that called the API must send the header, and must read `runId` instead of `token`
  from the launch response. `RunSummary` counts are now nullable, and `gate` takes the five
  states above. The API is v1 and unreleased, so these change in place.
- `error.json` joins the storage layout (ADR-0005). It is written only when there is no
  `result.json`.
- Not addressed here, and still open: the server re-reads every `result.json` on each poll; there
  is no structured logging beyond the `opseval.server` logger used for background failures; and
  the console is not yet served by `opseval serve` itself.
