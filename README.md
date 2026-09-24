<div align="center">

# Opseval

**Release-grade evaluation for conversational AI agents.**

Simulated customers · real protocol · human-approval checkpoints · verdicts you can trust

[![License: Apache-2.0](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)
![Status: pre-alpha](https://img.shields.io/badge/status-pre--alpha-orange)

</div>

> 🚧 **Pre-alpha.** The core CLI runs end to end (see Quickstart), but nothing is released on PyPI yet. See the [roadmap](ROADMAP.md) and the [architecture decisions](docs/adr/).

---

## What is Opseval?

Opseval is an open-source, **black-box evaluation framework** for conversational agents: support bots, commerce assistants and workflow agents that call tools and ask a human to approve before they act.

It tests your **deployed** agent the way your customers meet it:

1. **An LLM plays the customer.** Each customer comes from a persona and a script. It can write English or Arabic, including Gulf and Egyptian dialects, and makes realistic, reproducible typos.
2. **The conversation runs over your agent's real protocol:** HTTP, SSE, WebSocket, or any OpenAI-compatible endpoint. Opseval needs no access to your code.
3. **The simulated customer answers approval checkpoints** the way a person would: approve, reject with feedback, or wait on a status. Opseval then checks that nothing irreversible happened before permission.
4. **Every conversation is recorded as immutable evidence,** then graded by deterministic checks and auditable LLM judges that must quote their evidence.
5. **Each scenario runs several times** and folds into one verdict. The verdicts drive a **release gate** and **CI exit codes**.

### The promise

> **A problem with the test harness must never look like a problem with your agent,
> and a real agent failure must never be hidden as "could not measure".**

- Every score is `passed`, `failed` or `infra_error`.
- Every scenario verdict is `passed`, `failed`, `inconclusive`, `skipped_precondition` or `blocked`.
- An unmeasured value is shown as *not measured*, never as zero.
- An inconclusive run is never green in CI. Exit codes: `0` pass · `1` fail · `2` not judged · `3` harness error.

## Quickstart (from source, pre-release)

```bash
git clone https://github.com/Opsedia-ai/Opseval.git && cd Opseval
uv sync
uv run opseval demo            # starts a sample refund agent, runs the smoke suite, writes reports/<run>/report.html
uv run opseval demo --bug skip-approval   # the agent issues refunds without approval → release BLOCKED, exit 1
uv run opseval demo --bug crash           # the connection drops → "not judged", exit 3 (never a false red)
```

Point it at your own agent:

```bash
export OPSEVAL_ALLOWED_HOSTS=agent.example.com
uv run opseval validate my-scenarios/ --profile my-agent.profile.yaml
uv run opseval run my-scenarios/suites/release.yaml --profile my-agent.profile.yaml \
    --target wss://agent.example.com/chat
```

LLM personas and judges use any OpenAI-compatible endpoint: set `OPSEVAL_SIMULATOR_BASE_URL`, `OPSEVAL_SIMULATOR_MODEL`, `OPSEVAL_SIMULATOR_API_KEY`, and likewise `OPSEVAL_JUDGE_*`. Scripted scenarios need no LLM at all.

A scenario looks like this:

```yaml
id: refund-010
name: Reject the first refund proposal, then approve
goal: A customer with a damaged order wants a refund to the original card.
criticality: critical

persona:
  role: Returning customer, polite but firm about the refund method.
  language: ar
  dialect: egyptian
  typing_noise: 0.05

conversation:
  flow: >
    Say order 10482 arrived damaged and ask for your money back. If the proposal
    offers store credit, reject it and ask for the original card. Approve the
    corrected proposal.
  stop_at: supervisor_review          # stop before the irreversible refund
  checkpoint_policy:
    proposal_approval: {action: script, expected_target_phase: proposal}

expectations:
  expected_tools:
    must_call: [lookup_order, check_eligibility]
    forbidden_before:
      - {tool: create_refund, checkpoint: supervisor_review}

scorers:
  - {id: flow.rejection_routing, threshold: 1.0}
  - {id: judge.goal_achievement, threshold: 0.8}
  - {id: output.language_script, params: {script: arabic, min_fraction: 0.9}}

execution:
  repeats: 3
```

## How Opseval fits with other tools

| Tool type | Examples | Relationship |
|---|---|---|
| Observability | Langfuse, OpenTelemetry | **Complement.** Opseval reads traces to verify tool calls and pushes scores back. |
| Eval libraries | DeepEval, promptfoo, MLflow | **Complement.** Keep them for unit-level metrics. Opseval tests the deployed agent end to end. |
| Red-teaming | PyRIT, garak | **Integrate.** Opseval's deterministic gating checks act as the oracle for adaptive attacks. |

## Editions

Opseval is **open core**. All three editions run the same codebase:

| | Opseval OSS | Opseval Enterprise | Opseval Cloud |
|---|---|---|---|
| Licence | Apache-2.0 | Apache-2.0 core + commercial `ee/` modules | Managed service |
| Every evaluation feature | ✅ no usage limits | ✅ | ✅ |
| SSO enforcement, fine-grained RBAC, SCIM, audit logs, retention policies, compliance evidence packs | — | ✅ | ✅ |
| Who runs it | You | You | Opsedia |

Our pledge is in [GOVERNANCE.md](GOVERNANCE.md): **evaluation features are never moved behind a licence key.**

## Roadmap

| Release | Theme |
|---|---|
| v0.1 Alpha | CLI end to end: transports, SUT profiles, simulator (en/ar), checkpoints, scoring, verdicts, HTML report |
| v0.2 Beta | GitHub Action, pytest plugin, Langfuse traces, re-score, judge calibration, security judges |
| v0.5 | Self-hosted server and web console, Docker Compose and Helm |
| v1.0 | Stable schemas and APIs, security review, Arabic UI |

See [ROADMAP.md](ROADMAP.md) for details.

## Contributing

We'd love your help, especially with **language packs** (Levantine, Maghrebi, Turkish, Urdu, French…), **SUT profiles** for popular agent frameworks, and **scorers**. Read [CONTRIBUTING.md](CONTRIBUTING.md) and our [Code of Conduct](CODE_OF_CONDUCT.md).

## Security

Please report vulnerabilities privately. See [SECURITY.md](SECURITY.md).

## License

Apache License 2.0. See [LICENSE](LICENSE) and [NOTICE](NOTICE). Files under [`ee/`](ee/) are covered by a separate commercial licence.

---

<div align="center">Built by <b>Opsedia</b>.</div>
