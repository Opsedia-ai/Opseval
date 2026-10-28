# ADR-0062: Generated scenarios, as drafts only (flexibility level L3)

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

Writing the first twenty scenarios is the slowest part of adopting Opseval. Teams already have
material an LLM could turn into candidates: the agent's system description, product docs, and
transcripts of real conversations. Generating scenarios is cheap. Trusting them is not:

- **A generated scenario can be wrong in a way that passes.** A goal the agent was never meant to
  reach fails the agent unfairly. An expectation copied from the agent's own behaviour turns a bug
  into the reference.
- **Transcripts are customer data.** They may hold personal data and credentials.
- **The model that writes a scenario can share blind spots with the agent it tests.**
- **Cost grows quietly** when a button can be pressed many times.

The gate is only as good as its scenarios. So a machine may propose them, and only a person may make
them count.

## Decision

### `opseval generate` and the console button

```bash
opseval generate --from agent.md --profile profile.yaml --out drafts/ --count 10
opseval generate --from docs/ --task refunds --server URL      # server mode (ADR-0049)
opseval generate --from transcripts.jsonl --profile profile.yaml --out drafts/
```

- **Sources.** One Markdown or text file, a folder of them, or transcripts as JSONL (`{"speaker",
  "text"}` turns, one conversation per line).
- **The model.** A new LLM role, `generator`, with its own `OPSEVAL_GENERATOR_BASE_URL`,
  `OPSEVAL_GENERATOR_MODEL` and `OPSEVAL_GENERATOR_API_KEY` (a `SecretStr`). Its host is allowlisted
  like every other LLM endpoint. It goes through `llm.py`, so there is no vendor SDK
  ([ADR-0003](0003-python-core-openai-compatible-http.md)).
- **The console.** A "Propose scenarios" button on a task calls a server endpoint that runs the same
  code. The browser generates nothing.

### Drafts only

- **Files.** `generate` writes files under `drafts/` in the catalogue folder, which no suite
  includes by default.
- **Server mode.** Drafts are catalogue versions with the `draft` status. They never get a label.
- **Never labelled automatically.** Neither the CLI nor the server moves `latest`, `production` or
  any other label onto a draft. Promotion is a human action: moving the file into `scenarios/` and
  pushing, or "Accept" in the console. Acceptance is recorded with the user
  ([ADR-0050](0050-accounts-and-access.md)).
- **A draft that is run** (`opseval run drafts/x.yaml`) is reported as a draft in every output, so
  its verdict is not read as a release signal.

### Every draft passes the loader

- The model's output is parsed and checked with `catalogue.check_scenario_text`, the loader's own
  checks, plus the scorer params checks of [ADR-0043](0043-defaults-from-the-profile.md).
- A candidate that fails is retried once, with the loader's errors given back to the model. If it
  fails again it is dropped and counted ("3 of 10 candidates were invalid"). It is never written
  half-fixed.
- **The profile bounds what can be generated.** Checkpoints, tools and phases must exist in the
  profile, since the loader checks them. `allow_commit: true` is never generated. The commit guard
  ([ADR-0008](0008-committed-outcome-guard.md)) stays a human choice.
- **Expectations are written from the source, never from a run of the agent.** The generator does
  not see the agent's answers, so it cannot copy a bug into the expected result.
- Each draft gets `opseval validate`'s warnings ([ADR-0043](0043-defaults-from-the-profile.md),
  `advice.scenario_warnings`), shown next to it.

### Provenance

Each draft records, in a `provenance:` block in files and in the catalogue version in server mode:

- the generator model and its vendor family;
- the prompt hash (template and version);
- the SHA-256 digest of each source file;
- the transcript ids it drew on, if any;
- the time;
- the Opseval version.

The block survives acceptance, so a reviewer can always tell a generated scenario from a written
one. Analytics ([ADR-0052](0052-analytics-store.md)) can compare their failure rates.

### Transcripts: privacy and clean room

- Before a transcript reaches the generator, credential values are redacted with the same code as
  records ([ADR-0033](0033-redacting-credential-values.md)).
- Personal data is **not** removed by default, because the conversation is the subject matter. The
  CLI therefore refuses transcripts unless `--transcripts-contain-personal-data acknowledged` is
  given. The generator endpoint must be one the team may send that data to. `OPSEVAL_ALLOWED_HOSTS`
  is the control, and the docs say so.
- Drafts are told to replace names, emails, phone numbers and order ids with invented ones. A check
  flags any run of 20 or more characters copied verbatim from a transcript (`PROVENANCE_VERBATIM`)
  for the reviewer.
- Clean-room policy: the docs and the console say to use only material the team owns or may use.
  Opseval stores digests, never copies of sources.

### Cost cap

- `--count` (default 10, at most 50 per call) and `OPSEVAL_GENERATOR_MAX_TOKENS` per call bound one
  run. The usage meter records the `generator` role, so its spend shows separately in cost reports
  ([ADR-0066](0066-dashboards.md)).
- In server mode each task has a monthly generation budget. Once it is spent, the button and the
  endpoint refuse with the amount used. They never generate partially without saying so.
- A generator outage is a harness error (`LLMInfraError`, exit 3). It never touches any agent's
  verdict.

### Independence

When `profile.sut_model` is declared and shares a vendor family with the generator, `generate`
warns. It does not refuse: a draft is reviewed by a person before it counts, and the judges'
independence check ([ADR-0016](0016-judge-independence-and-quality-judges.md)) still applies when it
runs.

## Consequences

- Faster first catalogues, with a human still deciding what the gate measures.
- Review is the bottleneck by design. The console shows drafts with their source excerpt, warnings
  and provenance side by side.
- One more LLM role to configure, allowlist and pay for.

## Not decided here

- Automatic personal-data removal from transcripts (named-entity detection). It would need its own
  accuracy check before it could be trusted.
- Generating datasets (ADR-0060 rows) rather than whole scenarios.
