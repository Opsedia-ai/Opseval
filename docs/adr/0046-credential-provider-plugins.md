# ADR-0046: Credential provider plugins

- **Status:** Proposed
- **Date:** 2026-10-20
- **Deciders:** Opseval maintainers

## Context

FR-RUN-03 asks for pluggable credentials. Opseval has a static token
(`OPSEVAL_STATIC_TOKEN`), HS256 tokens minted per attempt (ADR-0012), and a token per test
tenant (ADR-0041). Agents behind single sign-on, an OAuth client-credentials flow or a vault
need tokens fetched from somewhere, and that code differs from one company to the next. It
belongs in a plugin, not in the core.

A credential sits on the core rule's boundary. If fetching a token fails, no request reached
the agent, so the attempt must not be judged. But a provider's own `TimeoutError` (its token
endpoint was slow) would reach a transport that reads `TimeoutError` as *the agent* not
answering. Without a guard, that would be scored as an agent failure.

## Decision

A plugin group, `opseval.credentials`. A plugin is a `CredentialProvider` class with a `name`
and a plain `def token_source(context) -> TokenSource`. `context` carries:

- the profile's name;
- the attempt's target;
- the tenant's id when a pool is in use;
- `value(name)`, which reads a named variable from the environment or the `.env` file Opseval
  was started with. Providers read their own settings and secrets this way: `.env` is never
  exported to `os.environ`.

`FetchedTokens` is a helper: a subclass implements `fetch() -> (token, ttl)`, and the token is
cached and fetched again before it expires.

- **Used only when named.** `OPSEVAL_CREDENTIALS=<entry-point name>`. Installing a package
  changes nothing, and only the named plugin is imported.
- **Discovery fails closed.** Each of these is a `ConfigError` (exit 3) before the first frame,
  never a fall-back to another credential, because a run that quietly used another identity
  would test the wrong account:
  - a name no installed plugin provides;
  - an import error;
  - a contract violation: not a subclass, the default name, an `async` `token_source`, or
    abstract methods left unimplemented;
  - a constructor that raises.
- **Every failure is a harness error.** The provider is wrapped in `GuardedTokens`. Anything
  it raises, and an empty or non-string token, becomes `CredentialsError`, a `HarnessError`:
  the attempt is not judged. The message names the provider and the exception *type* only,
  because a provider's error text can quote the credential it was handling. The source is
  built on first use, so a failure lands inside the attempt rather than between attempts.
- **Precedence.** A tenant's own `token_env` wins. Otherwise the provider supplies the token,
  in place of `OPSEVAL_STATIC_TOKEN`. With a provider, a profile's `token:` block may declare
  only `refresh_frame`: how a new token is sent on an open WebSocket connection. A block that
  mints (a TTL, claims or `refresh_before_seconds`) is refused at launch ("choose one"), so it
  is always clear which identity the agent sees.
- **Pools.** The provider is told each attempt's tenant. A tenant then needs neither
  `token_env` nor `target` to count as a distinct identity, and issuing one identity per
  tenant becomes the provider's job.
- **Preflight** asks the provider for one token, as an attempt would, for the first tenant it
  will serve. With a pool where every tenant has its own token, the provider is not asked. A
  failure stops the run with exit 3 before the first frame: every attempt would otherwise be a
  harness error, and the identity provider would be asked once per attempt for a suite that
  can judge nothing.
- **An injected transport with a provider is refused.** It takes no credentials, so the
  provider would silently do nothing.

## Consequences

- `current()` is synchronous and is called on the event loop, so a provider must cache. A slow
  fetch blocks the loop once per token lifetime. `FetchedTokens` keeps that to one call, and
  re-fetches at most half a lifetime early, so a short-lived token is not fetched on every use.
- On WebSocket, a provider's token is sent in `auth_frame` when a connection opens. Without a
  `token: {refresh_frame: …}` block it is only replaced on the next connection: an agent that
  rejects an expired token in-band would then be scored on that rejection. Declare
  `refresh_frame` whenever tokens outlive less than an attempt. HTTP transports ask for a
  token on every request.
- One provider instance per process, so a provider that opens a session in its constructor
  opens one.
- A profile that never asks for a token (no `auth_frame` on WebSocket) never consults the
  provider.
- Plugins run in-process with no sandbox, like scorer and sink plugins (ADR-0022, ADR-0027).
