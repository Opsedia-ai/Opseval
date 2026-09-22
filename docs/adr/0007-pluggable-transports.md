# ADR-0007: Pluggable transports beyond WebSocket

- **Status:** Proposed
- **Date:** 2026-09-22
- **Deciders:** Opseval maintainers

## Context

Most agents are reachable over plain HTTP, SSE or an OpenAI-compatible endpoint, not only over a stateful WebSocket. Adoption depends on pointing Opseval at an existing agent in minutes.

## Decision

Transports are plugins behind one `Transport` / `Session` contract. v0.1 ships `http-json`, `websocket` (handshake, auth, resume, token refresh), `openai-chat` and `python` (in-process callable). v0.2 adds `sse`. Everything platform-specific (frame kinds, checkpoints, phases, tools, close codes) lives in a declarative **SUT profile**, not in code.

## Consequences

- New platforms are supported by writing a profile, not by forking.
- Every transport must pass the transport contract test kit.
