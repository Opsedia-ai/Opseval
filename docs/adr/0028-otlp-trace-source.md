# ADR-0028: OTLP trace source

- **Status:** Proposed
- **Date:** 2026-10-05
- **Deciders:** Opseval maintainers

## Context

FR-INT-03 asks for an "OTLP trace source", and the risk register is explicit about why: R-08 lists
OTLP as the "vendor-neutral trace path" that keeps Opseval from depending on Langfuse staying
Langfuse.

There is a problem with the requirement as written. **OTLP is an ingest protocol and has no
standard query API.** You cannot "read traces over OTLP" the way you read them from Langfuse's
public API. Reading means choosing a mechanism, and the obvious candidates each have a cost:

| Option | Cost |
|---|---|
| Query Tempo's API | Picks a vendor — the thing OTLP is here to avoid |
| Query Jaeger's API | Same, plus Jaeger's query API is not an OTel standard |
| Run an OTLP receiver | Opseval becomes a server the agent's collector exports to; inverts the flow and needs a listening port during a test run |
| Read OTLP-JSON the collector wrote | Vendor-neutral, no credentials, no port — but needs the operator to configure a file exporter |

## Decision

Read the **OTLP-JSON** that the OpenTelemetry Collector's `file` exporter writes:
`profile.traces.source: otlp-file`, with the path in `OPSEVAL_OTLP_SPANS_PATH` (a file, or a
directory read recursively).

That is the only option that is vendor-neutral *by construction*, which is the whole point of
FR-INT-03 existing alongside the Langfuse reader. In CI the agent's collector writes spans beside
the run and Opseval reads them, with no backend, no credentials and no listening port.

Querying a backend is deliberately **not** built. The valuable, reusable part is the span →
`ToolCall` mapping below; a Tempo or Jaeger fetcher is a thin layer on top of it, and guessing
which backend to support would have been a product decision dressed as an implementation.

### Both file layouts are accepted

The collector's file exporter writes one envelope per line; a single pretty-printed document is
what people produce by hand or with the OTLP HTTP exporter. Both parse, because insisting on one
would make the feature fail for a reason that has nothing to do with the agent.

### An empty list is a positive claim

This is the rule that shaped the error handling. `[]` from a trace source means **"the agent
called no tools"**, and `tools.expected_called` acts on it. So an empty list is never returned
because reading failed:

- no file yet, or no spans for this session → `TraceUnavailable`, which the caller polls on and
  which ends as `infra_error` rather than a passing vacuous score;
- spans *did* exist for this session but none could be read → `TraceUnavailable` naming how many
  were unreadable, not `[]`. A missing `startTimeUnixNano` or an empty span name must not become
  "no tools were called";
- malformed lines are counted and skipped rather than aborting, so one bad line in a long export
  does not lose the run's evidence.

### Nothing about the mapping is guessed silently

There is no settled semantic convention for an agent session id, so the attribute is configurable
and three candidates are tried by default (`session.id`, `gen_ai.conversation.id`,
`opseval.session_id`), on the span **and** on the resource. The tool name comes from the span name
unless `gen_ai.tool.name` overrides it, which is what an HTTP-instrumented agent needs. Only OTLP
status code `2` (ERROR) means the call failed — `0` (UNSET) is not a failure, and treating it as
one would invent agent faults out of uninstrumented spans.

An undeclared tool name is kept rather than dropped, because `Evidence.unknown` already reports
those and silently discarding them would hide an agent calling something the profile does not know
about.

Verified end to end against the reference agent with a deterministic
`traces.session_id: "{test_id}-{attempt_index}"`: `trace_coverage` became `full`, three tool calls
per attempt were read, an ERROR span became `ok=False`, arguments decoded, and the manifest
recorded `otlp-file`.

## Consequences

- Opseval can verify tool calls with no Langfuse, which is what R-08 asked for. The Langfuse
  reader is now one of two, not the only way to get `trace_coverage: full`.
- **It needs the operator to configure a collector file exporter.** That is real setup, and for a
  team that already has Tempo it is more work than a query would have been. A backend fetcher
  reusing this mapping is the obvious follow-up.
- Correlation depends on the agent putting the session id on its spans. An agent that does not
  will produce `TraceUnavailable` forever, which reads as "traces not ingested yet" — honest, but
  indistinguishable from a slow exporter. `opseval preflight` checks the path exists; it cannot
  check that the attribute will be there.
- Reading is not incremental: every matching file is parsed on each fetch, and a fetch happens per
  attempt. On a long run with a large export that is repeated work, bounded by however much the
  collector wrote.
- `traces.source` is now a two-value union in the published profile schema, so an editor offers
  both. A third source would be a third value; moving this to the `opseval.trace_sources`
  entry-point group the architecture names is still open.
