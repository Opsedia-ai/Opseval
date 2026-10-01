# ADR-0016: Judge independence, and judges for policy, content quality and efficiency

- **Status:** Proposed
- **Date:** 2026-10-01
- **Deciders:** Opseval maintainers

## Context

Judges are themselves LLMs, and the LLM-as-judge literature is consistent on one point: a model
grades text from its own family more generously than a third party would (self-preference, or
self-enhancement, bias). So `judge.goal_achievement` run on the same vendor family as the agent
produces a number that looks like evidence and is not one. Nothing in Opseval noticed this.

That collides with the rule the whole harness is built on: a result we cannot trust must read
"not measured", never a quiet pass. A same-family pass is exactly a measurement we cannot trust.

Three things make this awkward:

1. Opseval is deliberately black-box. It drives an agent over its own protocol and cannot see
   which model is behind it. Only the operator knows.
2. The harness-axis judge (`judge.persona_consistency`, ADR-0014) does not grade the agent at
   all. It grades the *simulated customer*. Comparing it against the agent's model would be the
   wrong comparison and would miss the real conflict.
3. **`infra_error` is the wrong tool here.** `attempt_verdict` (ADR-0006) builds `measured` by
   *excluding* `infra_error` scores, and invalidates the attempt only when nothing at all was
   measured. A judge refused during scoring therefore disappears from the verdict: a scenario
   with one passing deterministic scorer still reports **passed, exit 0**, with the agent's
   behaviour silently ungraded. Worse, `persona_consistency` invalidates an attempt only on
   `FAILED`, so refusing *it* during scoring would switch off the harness safety net and let a
   broken simulated customer be reported as an agent failure — precisely what the one rule
   forbids.

Three judges from the PRD were also still outstanding: policy adherence (FR-SCR-06), which
ADR-0014 left as follow-up once the vote machinery existed, plus content quality and efficiency.

## Decision

**1. Model families.** `opseval.llm.model_family()` resolves a model id to a vendor family. The
**model id decides**; a deployment prefix is only a fallback:

- `openai/deepseek-chat` → `deepseek`. The `openai/` prefix is the standard LiteLLM spelling for
  "speaks the OpenAI dialect" (and Opseval is built entirely on such endpoints, ADR-0003), so it
  must never outrank the vendor named in the id. `openai` is excluded from prefix matching for
  this reason; every other vendor name is accepted there, so `us.anthropic.<new-id>` still
  resolves.
- Specific vendors are matched before `openai`, and `deepseek` before `meta`, so
  `deepseek-r1-distill-llama-70b` is `deepseek`.
- Whole-token matching catches bare reasoning ids (`o3`, `o1`) without matching them inside
  unrelated words.

It returns `None` when the family cannot be told. **`None` means unknown, never independent** —
no caller may read it as proof that two models come from different vendors.

**2. Declaring the agent's model.** `profile.sut_model`, and nothing else.
`transport_options["model"]` is deliberately *not* used as a fallback: many agent gateways ignore
that field (the scaffold ships `model: default`), so reading it would claim a model the agent
never ran, and produce a confident refusal against a fiction. The scaffolded `profile.yaml` ships
`sut_model` as a commented line so the knob is discoverable.

**3. What each judge is compared against.** The judge's family is compared with the family of the
model *that judge grades*: the simulator for `HARNESS`-axis judges, the agent for every other
axis.

**4. Enforced at launch, not per score.** `check_judge_independence` runs in `launch()` once the
clients exist and before the first frame is sent, because a family conflict is pure configuration:

| `OPSEVAL_JUDGE_INDEPENDENCE` | Behaviour |
|---|---|
| `error` (default) | `ConfigError` → **exit 3** (harness/config error), listing every conflicting scorer. Nothing runs, so nothing is paid for. |
| `warn` | Log the conflicts; affected scores are marked `degraded` with the pairing in `details`. |
| `off` | No check. |

An unrecognised mode is treated as `error`: the strict reading is the safe one, so a caller
passing `"STRICT"` cannot fail open to a scored pass.

Inside a judge a shared family **only** sets `degraded` and records the pairing — it never becomes
`infra_error`, for the reason in Context 3. Scoring therefore cannot lose coverage; refusing the
run is `launch`'s job. `opseval rescore` applies the same check, reading the simulator model the
run actually used from the manifest's `simulator_model` key (written from the live client, not
from `config_digest`, whose `simulator_model` is set whenever the env var is, even for a scripted
run that built no simulator).

**5. The check never fires on a guess.** If either family is unknown, there is no finding at all —
not an error, not a warning. A check that fired on suspicion would turn unknown configuration into
refused runs, and a run refused for an unverifiable reason is as dishonest as a fake pass.

**6. Three new judges**, reusing the existing quote-backed, weighted-criteria machinery:

- **`security.policy_adherence`** (security axis, 3 votes). Grades the agent against
  `expectations.policy`, a list of plain-language rules. Criteria: kept every rule (weight 3), did
  not help the customer work around one, declined plainly where a rule stopped it. A scenario that
  names this scorer without any rules is a **load-time error** (`check_references`), not a runtime
  surprise — the relationship is static, and CLAUDE.md requires authoring mistakes to fail before
  any agent traffic. With no rules it is also `NOT_MEASURABLE` at scoring time, for catalogues
  loaded by other paths. Because the default `threshold` is 1.0, any deduction fails, so the two
  conditional criteria instruct the judge to grade 1 when they do not apply and to reserve 0.5 for
  a partial breach; otherwise an agent that correctly refused would be reported as a breach.
- **`judge.content_quality`** (output axis). Grounded (weight 3), clear (weight 2), appropriate
  tone, actionable. Tool *results* are not in the judge's digest, so the prompt lists the tools the
  agent called and states that facts a listed tool plausibly supplied are grounded — otherwise a
  correct, tool-sourced amount reads as a hallucination and the harness's blind spot is charged to
  the agent.
- **`judge.efficiency`** (flow axis). No repetition (weight 3), direct path (weight 2), no
  stalling, with turn counts supplied as context.

**7. Rubric hashing.** `prompt_hash()` is a class property and cannot see the scenario, so a judge
whose criteria *are* scenario data reports `rubric_hash` in its details, and `opseval rescore`
compares it before reusing a score. Without this, editing `expectations.policy` and re-scoring
offline would reuse grades awarded against the old rules and present them as current — the one
thing ADR-0013 exists to prevent.

## Consequences

- A same-family pairing now **fails fast with exit 3** and an explicit message, rather than
  producing passes. Teams upgrading with judge and agent on one vendor must name another judge, or
  set `OPSEVAL_JUDGE_INDEPENDENCE=warn|off`. Exit 3 is the documented harness/config code, and it
  is reached before any conversation, so a misconfigured CI run is cheap.
- Because the check is a launch-time refusal rather than a per-score `infra_error`, it cannot
  silently reduce coverage, and it leaves the `persona_consistency` safety net intact.
- Existing judges keep their `version` and prompt hashes: their scoring logic is unchanged, and the
  only new in-judge effect is a `degraded` flag under `warn`. Results stay comparable across
  `opseval rescore`.
- Teams who never declare `sut_model` get no check. The honest reading is "independence unknown",
  and the field is documented that way — the protection is opt-in by construction, because a
  black-box harness cannot discover the agent's model on its own.
- `judge.content_quality` and `judge.efficiency` are strict against the default `threshold: 1.0`;
  the README tells teams to lower it rather than gate releases on style.
- `security.policy_adherence` costs three judge calls per attempt. Those ballots are still
  sequential, same-prompt, `temperature=0.0` calls that differ only by `seed` (ADR-0014's
  machinery), so on gateways that ignore `seed` they are not independent samples and
  `JUDGE_DISAGREEMENT` can rarely trigger. Making votes genuinely independent, and running them
  concurrently under the existing judge semaphore, is follow-up work that applies to every
  multi-vote judge, not just this one.
- `opseval calibrate` (judge ↔ human agreement) remains the natural follow-up: it would let a team
  *measure* judge bias on their own data instead of inferring it from the vendor name, at which
  point the family check becomes a cheap default rather than the only defence.
