# ADR-0056: Telephony

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

Many voice agents are reached only by phone: a contact-centre number, a SIP trunk, or a Twilio
number in front of an LLM. To test them as customers meet them, Opseval must place a real call.

That breaks an assumption the rest of Opseval relies on. Every transport so far is outbound:
Opseval connects to the agent. With Twilio, Opseval asks Twilio's REST API to place a call, and
Twilio then **connects back** to Opseval, both for call-status webhooks and for the Media Streams
WebSocket that carries the audio. So Opseval needs a public, reachable endpoint, while the console
API is loopback-only on purpose (ADR-0031). Phone minutes are also billed, and a phone number can
reach a real person.

## Decision

### A telephony plugin, behind the `AudioSession` contract

Telephony providers are loaded from the `opseval.telephony` entry-point group, named to be used
and failing closed, exactly like the speech providers (ADR-0054). A provider gives an
`AudioSession` (ADR-0053), so the bridge, the duplex driver (ADR-0055), the scorers and the
record are unchanged.

The first-party plugin is **Twilio**, over its REST API and Media Streams, through httpx and
websockets (no Twilio SDK, ADR-0003):

- `POST /Calls` places the call from an allowlisted test number to the agent's number, with
  TwiML that opens a `<Stream>` back to the gateway.
- Media Streams carry μ-law 8 kHz audio both ways; conversion is the in-house code from ADR-0053.
- `sendDigits` and in-stream DTMF give `send_dtmf`; status callbacks give `Transfer` and
  `Hangup`.

**SIP** is a plugin too, reserved but not built first. A SIP stack is a large dependency, and its
licence must pass the `make audit` allowlist before it is chosen.

### `opseval voice-gateway`, a separate process with its own security design

Twilio's callbacks are served by `opseval voice-gateway --public-url https://…`, not by
`opseval serve`. The console stays loopback-only. The gateway:

- **Requires TLS.** `--public-url` must be `https` (and `wss` for streams), and its host must be
  in `OPSEVAL_ALLOWED_HOSTS`. It serves TLS itself or sits behind a declared trusted proxy;
  anything else is refused at start (exit 3).
- **Verifies the signature of every request.** Each webhook and each stream handshake must carry
  a valid `X-Twilio-Signature` over the exact public URL and parameters, checked with the auth
  token (a `SecretStr`). An unsigned or badly signed request gets 403 and is logged without its
  body. There is no "skip verification" setting.
- **Accepts only calls it placed.** Each call is bound to one attempt by an unguessable token in
  the stream URL, checked against the call SID Opseval created. A stream for an unknown call is
  closed.
- **Serves nothing else.** No catalogue, no run data, no console. It holds audio only for the
  attempts in flight and hands it to the runner.
- **Talks to the runner over a closed channel.** The gateway↔runner link is loopback (a Unix
  socket or `127.0.0.1`) or, between hosts, mutually authenticated TLS with certificates both
  sides pin. Every message carries the attempt's token, and the runner accepts it only for the
  attempt that token was minted for. A message with an unknown or expired token is dropped and
  logged (token prefix and call SID only, no audio, no body). It never reaches an attempt, so a
  stray stream cannot become another attempt's agent audio.

### Test numbers only

- `profile.telephony.to` must match `OPSEVAL_TELEPHONY_ALLOWED_NUMBERS`, an explicit list. A
  number not on it is refused at load. There is no wildcard.
- The ADR-0008 commit guard applies: a call stops at `commit_gate` unless both the scenario and
  the run allow the commit.
- Opseval never dials a real customer. The docs say so in the first line of the telephony guide.

### The one rule, on the phone

On top of the ADR-0053 table:

| What happened | Result |
|---|---|
| Twilio's API errors (5xx, timeout, auth refused, rate limit) | `TransportError` (harness): not judged |
| Our side is misconfigured (the from-number is not ours, the TwiML or stream URL is refused, an invalid `to` format) | `ConfigError` (exit 3) at launch or preflight, else `TransportError`: not judged |
| The call is placed and the agent's number is busy | `TransportTimeout`, naming `busy`: the agent's fault |
| The agent's number rejects the call | `TransportTimeout`, naming `rejected`: the agent's fault |
| The call is placed and nobody answers | `TransportTimeout`, naming `no-answer`: the agent's fault |
| The media stream drops mid-call | `TransportClosed`: not judged |
| The agent hangs up before the goal | a recorded `Hangup(by=agent)`, scored as behaviour |
| The agent transfers the call | a recorded `Transfer`, scored against the scenario's expectation |

Capacity is part of what is tested: a contact-centre number that is busy, rejects calls or rings
out would do the same to a customer. So once Twilio reports that it placed the call, a busy,
rejected or unanswered call is the agent's, named by its Twilio status. What comes before that
(Twilio's own API, Opseval's account, numbers and URLs) is the harness's. The split follows
Twilio's call status and error codes, never message text: carrier and provider errors (the
`failed` status with a Twilio error code for the provider or the account) are harness errors.
A `to` number Twilio says does not exist is on our own allowlist, so it is a configuration
problem, not the agent's: `TransportError`, not judged.

### Cost

The call's price and duration come from Twilio's call record after hang-up and go into the usage
meter as role `telephony`. A price Twilio does not report is `None`, never 0. A suite's
`stop.max_wall_clock_seconds` bounds a run's minutes, and a per-run call cap
(`OPSEVAL_TELEPHONY_MAX_CALLS`) refuses a suite that would exceed it at launch.

## Consequences

- Phone agents are testable end to end, including DTMF menus and transfers.
- Opseval has, for the first time, an internet-facing listener. It lives in a separate process
  with a narrow surface, so the console's loopback posture (ADR-0031) is unchanged.
- CI cannot place real calls. A fake Twilio (REST plus a Media Streams client) runs on loopback
  for the fault-injection tests; a nightly job calls a test number.
- Every run spends money. The cap and the cost line make it visible before and after.

## Not decided here

- Other telephony providers (Vonage, Telnyx, a contact-centre platform's own API). Each is a
  plugin under the same group and the same rules.
- Whether the gateway joins the container image (ADR-0038). Today the image ships the CLI only.
