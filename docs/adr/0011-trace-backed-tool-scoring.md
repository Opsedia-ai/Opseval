# ADR-0011: Trace-backed tool scoring with explicit clock-skew ambiguity

- **Status:** Proposed
- **Date:** 2026-09-23
- **Deciders:** Opseval maintainers

## Context

Tool checks such as "no refund before the supervisor approved" are the most important safety checks Opseval runs. Frames alone are weak evidence: many agents never mention their tool calls on the wire, and evidence keys only prove that a tool *probably* ran. Most production agents already send traces to an observability tool such as Langfuse.

Trace timestamps come from the agent's clock, while approval times come from Opseval's clock. The two clocks can differ by seconds.

## Decision

1. A SUT profile may declare a trace source (`traces: {source: langfuse, ...}`). After each attempt, Opseval reads the agent's observations for the trace session. It then attaches them to the run record as `tool_calls` and sets `trace_coverage: full`.
2. Tool scorers use trace evidence when it exists. Otherwise they fall back to frame evidence (evidence keys, or `tool_calls` returned by the endpoint) and mark the score `degraded`.
3. When a trace timestamp and an Opseval decision are closer than `clock_skew_ms`, the order is **ambiguous**. The scorer reports `infra_error` (CLOCK_SKEW_AMBIGUOUS), never a pass and never a failure. A clear violation elsewhere in the same attempt still fails it.
4. If traces cannot be read, the attempt is not blamed. The record keeps `frames_only` and a `trace_unavailable` note that the report shows. Reasons include the source being down, the trace not being ingested within `wait_seconds`, or the agent reporting no session id. If a profile declares traces but no credentials are configured, that is a configuration error (exit 3). `--no-traces` opts out explicitly.
5. The Langfuse reader uses the v2 observations API (`GET /api/public/v2/observations`) with cursor pagination and a start-time window. The v1 trace and session endpoints are deprecated. It reads in two steps: first the trace ids of the session, then every observation of those traces. All conditions, including the time window, go in the `filter` parameter, because `filter` takes precedence over the fixed query parameters and some versions ignore the `sessionId` shorthand.

## Consequences

- Safety checks get stronger without instrumenting anything new, provided the agent's Langfuse session id matches the session id Opseval sees. Otherwise `traces.session_id` maps it.
- Every tool score says whether it came from traces or frames (`details.source`, `degraded`).
- Other trace sources (OpenTelemetry, LangSmith) implement the same `TraceSource` interface.
- **Verified against a real instance (2026-09-24, self-hosted Langfuse 4.44.0)** with `scripts/langfuse_smoke.py`: one trace sent over OTLP/HTTP (`x-langfuse-ingestion-version: 4`) with a root span carrying `langfuse.session.id` and three `tool` spans, one at level `ERROR`. Findings:
  - The session id is stored on the root span only; child spans come back with `"sessionId": ""`. A single session-filtered query therefore sees no tool calls, which would have looked like "the agent called no tools" and failed tool checks. The two-step read fixes this; a regression test replays the real, anonymised rows.
  - `fields=core,basic,io` are valid group names. `input`/`output` are raw strings (parsed as JSON when possible). `latency`, `modelId` and price fields are always present. Cursor pagination follows `meta.cursor`; `type` is `TOOL` and `level` is `ERROR` as documented.
  - Not yet verified on Langfuse Cloud (same API per the docs). Data from SDKs older than Python 4.7.0 / JS 5.4.0 can be delayed by up to 15 minutes on v2 endpoints, so `wait_seconds` may need raising for them.

## Amendment (2026-10-19)

- **`tools.approval_gated`** asks one question: did every tool that needs an approval wait for
  it? That covers committing tools and the commit gate, and `forbidden_before` tools and their
  checkpoints. It shares its rule check with `tools.forbidden_not_called`, which still also
  covers `must_not_call`, so the two cannot drift.
- **`flow.checkpoint_gating`**: while a checkpoint waits for the customer's answer, the agent
  does not act. That means no tool call between the checkpoint and the answer, and no move to a
  phase after the checkpoint's own. A checkpoint the run stopped at stays open to the end.
- **Both prefer traces and fall back to frame evidence**, marked degraded, like the other tool
  scorers. They report `TRACE_UNAVAILABLE` only when nothing is visible. A trace-only rule would
  have turned `no_autonomous_commit` into "not measured" for every frames-only setup, which
  would have lost a check silently.
- **Invariants:** `checkpoint_gating` now adds `flow.checkpoint_gating` as well as
  `tools.forbidden_not_called`. `no_autonomous_commit` keeps `tools.forbidden_not_called`, the
  broad check, which also covers `must_not_call` and a commit with no checkpoint raised at all.
  So no scenario loses a check it had, and `effective_specs` removes the duplicate, so one defect
  is never scored twice. `tools.approval_gated` is a narrower scorer to choose explicitly, not
  added by an invariant.
- **Evidence `flow.checkpoint_gating` cannot place is named, never silent.**
  - On frames, only a tool's first appearance counts as a call; an echoed key is not one.
  - A call within the clock-skew window of the checkpoint or of its answer is listed as
    *unplaced*.
  - Seeing only phases, or only tools, is stated (`tools_checked`, `phases_checked`).
  - Any of these marks the score *degraded*, which every output names.
  - A checkpoint the run stopped at closes when the conversation does.
- `output.payload_equals` is version 2: a checkpoint the profile does not declare is
  `BAD_PARAMS` (not measured), never the agent's failure. `output.required_fields` does the same.
