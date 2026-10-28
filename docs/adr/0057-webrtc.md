# ADR-0057: WebRTC

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

Many voice agents are built on WebRTC rooms rather than phone lines or plain WebSockets. LiveKit
and Daily are the common hosts: the agent joins a room as a participant, and a client joins the
same room to talk to it. To test those agents as a user meets them, Opseval must join the room as
the caller.

WebRTC is the riskiest voice channel to add:

- **Dependencies.** A Python WebRTC stack (ICE, DTLS, SRTP, RTP) is large. The practical choice,
  aiortc, depends on PyAV, whose wheels bundle FFmpeg. FFmpeg's licence depends on how it was
  built.
- **Codecs.** Rooms carry Opus. Decoding it needs libopus or a binding to it.
- **Licences.** `make audit` admits only MIT, BSD, Apache, ISC, PSF and MPL-2.0 licences
  (`ALLOWED_LICENCES` in the `Makefile`). LGPL or GPL anywhere in the tree fails it, as psycopg did
  for ADR-0047.
- **Vendor SDKs.** LiveKit and Daily ship SDKs, and ADR-0003 rules them out of the core.

## Decision

### An optional `voice-webrtc` extra

- WebRTC lives in `opseval[voice-webrtc]`. The base install, the `voice` extra and the container
  image (ADR-0038) do not include it.
- An import-linter contract keeps the rest of Opseval from importing it, as for the `server`
  extra.
- The stack is **aiortc**, with **Opus decoding through a library that passes the licence
  allowlist**. The extra is added only if `make audit` passes on the whole resolved tree,
  including what PyAV's wheels bundle. If it cannot pass, the extra is not shipped. We do not
  widen the allowlist to make it pass.

### In-house signalling clients

Joining a room is two steps: get a token from the room host's API, then exchange SDP with its
signalling server. Both are written in-house over httpx and websockets:

- **LiveKit:** a room token (a JWT signed with the project's API secret, minted in-house as the
  HS256 tokens are in `auth.py`) and LiveKit's signalling WebSocket.
- **Daily:** a meeting token from Daily's REST API, then its signalling.
- Each is a plugin under `opseval.telephony` (ADR-0054, ADR-0056), so a third host is a plugin
  too. API keys are `SecretStr`, and every host is allowlisted with TLS.

Each client gives an `AudioSession` (ADR-0053). The bridge, the duplex driver (ADR-0055), the
scorers and the record are unchanged.

### The one rule, in a room

On top of the ADR-0053 table:

| What happened | Result |
|---|---|
| The room API refuses the token, or 5xx | `TransportError` (harness): not judged |
| ICE or DTLS never completes | `TransportError`, naming the stage: not judged |
| Connected, but the agent never joins the room | `TransportTimeout`: the agent's fault |
| The agent joins but publishes no audio track within the turn timeout | `TransportTimeout`: the agent's fault |
| Media stops mid-call and the connection fails | `TransportClosed`: not judged |
| Opus packets that do not decode | synthetic `error` frame, packets kept: the agent's fault |

A NAT or firewall that blocks ICE is a harness problem, and it must not look like an agent that
said nothing. That is why ICE failure is named by stage and not judged.

## Consequences

- LiveKit and Daily agents become testable, with all the voice metrics of ADR-0055.
- This is the voice channel with the most dependency and licence risk, which is why it comes last
  in the voice track. It must pass `make audit` before it merges, and it is re-checked on every
  dependency update.
- CI runs a loopback room: a minimal signalling server and an aiortc peer as the agent, both on
  127.0.0.1. No vendor account is needed for the fault-injection tests.
- Room hosts change their signalling protocols. Keeping the clients in-house means we follow
  those changes ourselves.

## Not decided here

- Which Opus binding. It is chosen when the extra is built, by the allowlist.
- Video tracks. Opseval evaluates speech; a video track is ignored and noted in the record.
- TURN servers. Teams that need one configure it; Opseval does not run one.
