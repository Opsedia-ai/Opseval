# ADR-0012: WebSocket resume and short-lived tokens

- **Status:** Proposed
- **Date:** 2026-09-24
- **Deciders:** Opseval maintainers

## Context

Long conversations over WebSocket meet dropped connections (deploys, load balancers, idle timeouts) and expiring credentials. Before this decision any drop ended the attempt as `transport_failure`, and credentials were one static token. That is safe, but it hides agents that drop connections and cannot test agents whose tokens live shorter than a conversation.

Two failure modes pull in opposite directions:

- A drop the harness cannot recover from must not look like an agent failure.
- A drop the agent caused must not disappear just because Opseval recovered from it.

A drop also leaves one message in doubt: a frame sent as the connection closed may or may not have reached the agent.

## Decision

1. **Opt-in resume.** A SUT profile may declare `reconnect: {resume_session_frame, max_attempts, backoff_seconds, confirm_timeout_seconds, resend_unanswered}`. On a drop, Opseval opens a new connection, authenticates, sends `resume_session_frame` (values `{session_id}`, `{token}`) and waits for a handshake frame that carries the **same** session id. The loader rejects `resume_session_frame` without `session_id_field`.
2. **When a drop stays a transport failure (not judged):** there's no `resume_session_frame`, or no session id is known yet. It also applies when the agent answers with a different session id ("resume refused"), when nothing confirms the session within `confirm_timeout_seconds`, or when every attempt fails.
3. **The message in doubt.** If the connection dropped while a sent frame was unanswered, `resend_unanswered: true` resends it once after the resume. Without it, a later silence is a **transport failure**, not an agent timeout, because the message may have been lost in the drop. With it, silence after the resend is the agent's timeout.
4. **Evidence.** Run records gain `transport_events` (`dropped`, `resumed`, `resume_failed`, `token_refreshed`) with close codes and attempt numbers, never token values. A new opt-in scorer, `conversation.connection_stable` (flow axis), fails when the agent dropped the connection even if the session resumed (`params.ignore_codes` tolerates chosen close codes).
5. **Short-lived tokens.** A profile may declare `token: {ttl_seconds, refresh_before_seconds, claims, refresh_frame}`. Opseval then mints HS256 JWTs signed with `OPSEVAL_TOKEN_SIGNING_KEY`, one token source per attempt. Tokens are refreshed `refresh_before_seconds` before expiry, including while Opseval is waiting for a reply. With `refresh_frame` the fresh token is sent on the open connection. Without it, the fresh token is used on the next connection. HTTP transports send a current bearer token on every request. A profile `token` without a signing key is a configuration error (exit 3).
6. Handshake frames never count as the start of the agent's reply (a late resume confirmation must not end the agent's turn).

## Consequences

- Profiles without `reconnect` or `token` behave exactly as before. The demo profile is unchanged, so `opseval demo --bug crash` still exits 3.
- Teams can gate on connection stability with `conversation.connection_stable` without every resumed drop failing every scenario.
- `resend_unanswered` is off by default because duplicates can be harmful (e.g. a second refund request); agents must opt in.
- JWT minting uses the standard library only (no new dependency). Only HS256 is supported; asymmetric keys would need a new credential provider.
