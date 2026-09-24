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
5. The Langfuse reader uses the v2 observations API (`GET /api/public/v2/observations`) with cursor pagination and a start-time window. The v1 trace and session endpoints are deprecated. The session filter uses the `filter` parameter, because some versions ignore the `sessionId` shorthand.

## Consequences

- Safety checks get stronger without instrumenting anything new, provided the agent's Langfuse session id matches the session id Opseval sees. Otherwise `traces.session_id` maps it.
- Every tool score says whether it came from traces or frames (`details.source`, `degraded`).
- Other trace sources (OpenTelemetry, LangSmith) implement the same `TraceSource` interface.
