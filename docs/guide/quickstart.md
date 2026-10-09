# Quickstart

Opseval needs Python 3.11 or newer. Before the first release it installs from a checkout:

```bash
git clone https://github.com/Opsedia-ai/Opseval.git && cd Opseval
uv sync
```

## See it work

`opseval demo` starts a small reference refund agent, runs a suite against it, and writes a
report:

```bash
uv run opseval demo                         # a healthy agent: exit 0
uv run opseval demo --bug skip-approval     # refunds without approval: BLOCKED, exit 1
uv run opseval demo --bug crash             # the connection drops: not judged, exit 3
```

Open `reports/<run>/report.html` to see each scenario, each attempt and each check. The third
run is the point of the whole tool: the agent did not get to answer, so nothing is blamed on it.

## Point it at your agent

`opseval init` writes a starting catalogue: a profile, example scenarios and two suites. In a
terminal it asks what kind of agent you have, its URL, a name, and the model it runs on, and puts
the answers in the files (the URL's host goes into the allowlist in `.env.example`). Flags answer
the same questions (`--target`, `--name`, `--sut-model`), and without a terminal (in CI) it never
asks. It validates what it wrote and prints the command to run next.

```bash
uv run opseval init my-evals
export OPSEVAL_ALLOWED_HOSTS=agent.example.com
uv run opseval validate my-evals/suites/smoke.yaml --profile my-evals/profile.yaml
uv run opseval run my-evals/suites/smoke.yaml --profile my-evals/profile.yaml \
    --transport openai-chat --target https://agent.example.com/v1 --junit junit.xml
```

Every host Opseval talks to (your agent, any LLM, Langfuse, a webhook) must be listed in
`OPSEVAL_ALLOWED_HOSTS`, local addresses included, and use TLS unless it is on loopback.

- An agent that speaks the OpenAI Chat Completions API works with `--transport openai-chat` and
  the built-in profile.
- For WebSocket, HTTP-JSON or Server-Sent Events agents, write a [SUT profile](profile.md).

## Add an LLM

Scripted scenarios need no LLM. Persona scenarios and judges use any OpenAI-compatible
endpoint:

```bash
export OPSEVAL_SIMULATOR_BASE_URL=https://api.example.com/v1 OPSEVAL_SIMULATOR_MODEL=...
export OPSEVAL_JUDGE_BASE_URL=https://api.other.example.com/v1 OPSEVAL_JUDGE_MODEL=...
```

A judge from the same vendor family as the model it grades is refused at launch
(see [concepts](concepts.md#independent-judges)). Check everything before a long run with
`opseval preflight`.

## Exit codes

| Code | Means |
|---|---|
| `0` | Passed |
| `1` | The agent failed something the gate blocks on |
| `2` | Not judged: too little could be measured to decide |
| `3` | A harness or configuration problem: nothing was blamed on the agent |
| `130` | Interrupted (Ctrl-C) |
