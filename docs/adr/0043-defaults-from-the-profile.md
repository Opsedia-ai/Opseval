# ADR-0043: Defaults from the profile, and checks that say what they take

- **Status:** Proposed
- **Date:** 2026-10-21
- **Deciders:** Opseval maintainers

## Context

The 2026-10-20 adoption review drove four kinds of agent through Opseval by hand. It found that
the defaults assume the reference refund agent, and that some authoring mistakes pass silently:

- **`scorers: default` assumed phases and tools.** It always included `flow.phase_order`, which
  is *not measured* without phases, and `tools.forbidden_not_called`, which passes vacuously
  without tools. A chatbot on the defaults could never read "Safe to release". And a scenario's
  `must_call` or `ordering` was **never checked** under `scorers: default`: the pack had no scorer
  for either.
- **`stop_at` defaulted to `commit_gate` and failed to load without one.** Every chatbot scenario
  had to write `stop_at: goal_reached`.
- **A silent agent passed (ATTENTION I10).** The scaffolded smoke checks (`no_errors`,
  `terminal_state`, `turn_budget`) cannot tell an agent that answers every turn with an empty
  string from one that answers well. ADR-0042 caught a wrong *mapping*; nothing caught a silent
  *agent*.
- **Scorer `params` were never checked at load (I15).** A typo (`budjet`) was ignored, and a bad
  value became `BAD_PARAMS` at run time: *not measured*, then a warning. That contradicts the
  rule that authoring mistakes fail at load time.
- **`expectations.kind: refusal` was accepted and never read.** A refusal scenario was graded as
  if the agent should have done what the customer asked.
- **Running out of scripted turns ended the attempt `goal_reached`.** That reads as evidence the
  goal was met, when it only means the script was used up.

The guided scenario form (D14) depends on the first and fourth fixes: it must not offer checks
that cannot measure anything here, and it needs each check to describe its settings.

## Decision

### `scorers: default` follows the scenario and the profile

`default_pack(scenario, profile)`:

- **Always:** `conversation.no_errors`, `conversation.agent_replied` (below) and
  `output.terminal_state`.
- **`flow.phase_order`:** only when the profile declares phases.
- **`tools.forbidden_not_called`:** when the scenario has `must_not_call` or `forbidden_before`
  rules, or the profile has a commit gate with a committing tool.
- **`tools.expected_called`:** when there is a `must_call`.
- **`tools.ordering`:** when there are `ordering` rules.

Every declared expectation is therefore checked.

`effective_specs(scenario, profile)` takes the profile, and every caller that has one passes it:
the engine, the runner's dependency skips, rescore, advice and the independence check. They
therefore agree on one set. Without a profile (preflight's dependency counting, and older
callers), the previous pack is used. Those callers only ask whether a scenario's checks all need
a judge or traces, and both packs answer that the same way.

### `stop_at` left out means "stop before the irreversible step, if there is one"

- **Left out, with no commit gate:** the conversation runs to its goal, and
  `output.terminal_state` (now version 2) expects `goal_reached`.
- **Written out as `stop_at: commit_gate` on such a profile:** still a load error. It asks for a
  gate that does not exist.
- **Telling the two apart:** Pydantic's `model_fields_set` records whether the field was given,
  so the schema is unchanged.
- **The commit guard (ADR-0008) is untouched:** wherever a gate exists, the default stops at it.

### `conversation.agent_replied`: a silent agent fails

The check is agent-facing (flow axis). Every customer turn, a message or a checkpoint answer,
must be answered before the next one by one of these:

- **words:** text that is not blank once redaction markers are removed;
- **a checkpoint, a result or an error.**

The check fails in these cases:

- **Empty or whitespace answers.** An empty string, whitespace, or a `null` text field is the
  agent saying nothing.
- **No answer before the timeout.** That turn is unanswered, as the `TIMEOUT` already says.

Nothing sent at all is a vacuous pass, named as one. The check is in the default pack and in
every scaffolded scenario. It closes I10, and it is the other half of ADR-0042:

- **A wrong mapping** is not judged (`protocol.replies_read`, harness axis).
- **A mapping that reads an empty answer** fails the agent.

### Every scorer declares its `params`, checked when a scenario loads

`Scorer.Params` is a Pydantic model; `extra="forbid"` and `strict=True` come from the shared
`NoParams` base.

- **Every built-in declares one.** `NoParams` is for those that take none, and a test enforces
  it.
- **`check_references` validates each scenario's `params` against it** when given the registry,
  which every caller now passes. Iterating the registry still yields the ids, so code that
  passes ids keeps working.
- **Problems are reported at the field path,** e.g. `scorers.0.params.budjet`, by
  `opseval validate`, the loader, the console's editor and import.
- **Strict on purpose.** At run time `bool("false")` is `True`, so a lax model would let the load
  check and the run disagree.
- **Plugins:** a plugin scorer without a `Params` model is left unchecked, so existing plugins
  keep working, and `validate` warns about it.
- **Where the settings show:** `describe_params` lists them (`name*` = required), in
  `opseval list-scorers` and in the generated scorer reference. The form will build on it.

### `expectations.kind: refusal` reaches the judges

For a refusal scenario, every judge using the shared context is told the correct outcome is that
the agent declines and does not carry out the request, and `rubric()` gains `kind=refusal`.

- **Completion scenarios:** the prompt and the rubric digest are byte-identical, so earlier
  judge scores stay reusable by `rescore`.
- **Refusal scenarios:** their digest changes, so a refusal graded the old way is never reused.

### Running out of scripted turns is said for what it is

- **The signal:** the scripted simulator returns `script_exhausted`.
- **What the driver records:** the note `script_exhausted` (`models.record.SCRIPT_EXHAUSTED`)
  and the stop reason "the scripted turns ran out".
- **What `output.terminal_state` adds:** a note saying this shows the conversation ran to its
  end, not that the goal was met. It still passes: the turns were delivered without an error, a
  timeout or a stop.

What a scripted scenario cannot show, `conversation.agent_replied` and the content checks still
to come will.

## Consequences

- A chatbot on `scorers: default` can read "Safe to release". A scenario's tool expectations are
  checked under the default pack, so a scenario that relied on them being skipped can now fail
  or come out *not measured*.
- Scenarios that were loading with bad or misspelt `params` now fail `validate`. That is the
  point: those checks were *not measured*, or wrongly configured, on every run.
- `output.terminal_state` is version 2. `opseval rescore` reports the change, and re-scoring an
  old gate-less run moves that check from *not measured* to a real result.
- The demo's exit codes are unchanged (0 / 1 / 1 / 3). Its scenarios list their scorers, and its
  profile has a commit gate.
- Next: the console API serves each check's `Params` JSON Schema to the guided scenario form (D14).
