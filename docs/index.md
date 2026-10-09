# Opseval

Open-source, black-box evaluation for conversational AI agents.

A simulated customer (a script, or an LLM playing a persona) talks to your agent over its real
protocol, answers its approval checkpoints, and every attempt is recorded. Deterministic checks
and quote-backed LLM judges score each attempt. Repeated attempts fold into a verdict per
scenario, then a release gate and a CI exit code.

**The promise:** a problem with the test harness never looks like a problem with your agent,
and a real agent failure is never hidden as "could not measure".

- New here? Start with the [quickstart](guide/quickstart.md).
- Want the vocabulary first? Read [concepts](guide/concepts.md).
- Connecting your own agent? See the [SUT profile guide](guide/profile.md).
- Looking for a recipe? The [cookbook](guide/cookbook.md) has CI gating, security tests,
  stopping early, retention and containers.
- Every scorer and every setting: the [reference](reference/scorers.md), generated from the
  code.
- Why things are the way they are: the [architecture decisions](adr/README.md).
