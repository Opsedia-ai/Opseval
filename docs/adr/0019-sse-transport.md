# ADR-0019: Server-Sent Events transport

- **Status:** Proposed
- **Date:** 2026-10-02
- **Deciders:** Opseval maintainers

## Context

Streaming over `text/event-stream` is the common shape for agents that are neither a
request/response JSON endpoint nor a WebSocket: the client POSTs a message and the server streams
the turn back as events. Without it, those agents could only be tested through `http-json`, which
reads one JSON body and so sees nothing of a streamed turn.

The interesting part is not the parsing. It is which failures belong to the agent and which belong
to Opseval, because a stream gives more ways to go wrong than a single response does, and
ADR-0006 makes the distinction load-bearing: a `TransportTimeout` counts against the agent, while
anything under `HarnessError` invalidates the attempt instead.

## Decision

`SseTransport` (`--transport sse`) posts the outbound frame with `Accept: text/event-stream`,
consumes the stream for that turn, and queues the decoded frames. `receive` hands them out and
raises `TransportTimeout` once they are exhausted — the same contract as the request/response
transports, so the driver cannot tell them apart.

**Attribution of faults:**

| What happened | Result | Why |
|---|---|---|
| Stream stalls past `request_timeout` | `TransportTimeout` | The agent did not finish its turn. Counts against it. |
| Connection refused, reset, or dies mid-stream | `TransportClosed` (a `HarnessError`) | Opseval could not keep the session. Never the agent's fault, so the attempt is *not judged*. |
| HTTP 5xx | synthetic `error` frame | The agent answered, with an error. That is behaviour, and it gets scored. |
| HTTP 4xx | `TransportError` | Opseval's request was rejected: a harness or configuration problem. |
| Response is any content type other than `text/event-stream` | `TransportError`, naming `--transport http-json` when it was JSON | A JSON body, a proxy's HTML login page or a redirect parses to zero events, and an empty turn is scored against the agent. |
| Undecodable event payload | `_raw` frame | Recorded verbatim; it can never be mistaken for a clean turn. |

**The event name may supply the frame kind.** Many SSE agents put the message type in `event:` and
the payload in `data:`. The event name is used only when the payload does not already declare the
profile's `kind_field`, so a profile-driven kind always wins.

It is **never** stamped onto an undecodable payload. Giving a `_raw` frame a kind would let the
profile classify it as, say, a `message` with no text — turning garbage into something that looks
like a clean, empty turn. That is exactly the failure mode the one rule exists to prevent, so the
`_raw` frame stays unclassified and surfaces as `unknown`.

**Only an absent content type is tolerated.** Some gateways omit the header, and refusing there
would invent a harness failure for a server that works. Anything else that is not
`text/event-stream` is refused, because it parses to zero events and an empty turn is scored
*against the agent* — so a proxy login page, an auth redirect or the wrong URL would be recorded
as the agent failing to answer. That inversion is what the one rule forbids, and it is worth more
than tolerating a mislabelled but working stream.

**An empty stream is a turn with nothing in it**, not an error: the agent answered and chose to
say nothing, which the scorers can judge. A stream that ends without a trailing blank line still
dispatches its last event, because truncation is common and dropping that event would lose agent
output.

The field grammar follows WHATWG: multiple `data:` lines join with newlines, one leading space
after the colon is stripped, `:` comments and data-less blank lines are heartbeats rather than
events, and `retry:` is ignored because Opseval reconnects per attempt rather than per stream.
Parsing never raises.

## Consequences

- Streaming agents are testable without a bespoke profile hack, and the driver, scorers, records
  and reports need no changes at all.
- One `send` blocks until the agent's turn ends. The turn is bounded by `request_timeout` through
  an explicit `asyncio.timeout` around the whole stream, not by httpx's `timeout` argument, which
  bounds each individual read: an agent trickling keep-alive comments would satisfy every read and
  hold the turn open indefinitely. A turn is also capped at `max_turn_bytes` (32 MiB), since the
  stream is buffered before parsing and an endless stream would otherwise grow the harness heap.
  Both limits surface as `TransportTimeout`, which is the agent's fault. Bounded this way rather
  than by `turn_quiet_ms`. An agent that holds the stream open after it has finished speaking
  therefore reads as a timeout; `turn_end_field` does not shorten the wait, because the transport
  consumes the whole stream before handing any frame over.
- Frames are not surfaced incrementally. Nothing in the harness needs partial turns today, and
  buffering keeps the `_QueuedSession` contract, but a future live view (v0.5) would want them
  streamed as they arrive.
- Session resume and token refresh (ADR-0012) are WebSocket-only. An SSE session gets a fresh
  bearer on each POST through the existing `TokenSource`, which is the natural fit for a
  per-request transport, but a dropped stream mid-turn is not resumable: it is a harness failure
  and the attempt is not judged.
- `--transport sse` sends credentials, so it goes through `check_url_allowed` with TLS required
  for non-loopback hosts, like every other networked transport.
