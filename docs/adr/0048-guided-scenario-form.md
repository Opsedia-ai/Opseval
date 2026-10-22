# ADR-0048: The guided scenario form: the server offers the choices and writes the YAML

- **Status:** Proposed
- **Date:** 2026-10-22
- **Deciders:** Opseval maintainers (the owner asked for scenarios to be added mainly from the
  console on 2026-10-21; ATTENTION D14)

## Context

Today a scenario is added by writing YAML, either in a file or in the console's text editor
(ADR-0029). The people who know what to test should not need YAML: QA, conversation designers
and product owners. The form needs four things:

- each choice it can offer: the profile's checkpoints, phases and tools, the languages, the
  checks, and the settings each check takes;
- a way to turn what was filled in into a scenario;
- a judgement on whether that scenario is valid;
- a place to save it.

The console's two rules apply to all four:

- **The browser computes nothing.** A choice or a rule decided in the browser is one no Python
  test covers, and one that drifts from the loader.
- **There is no UI without an endpoint.**

ADR-0043 gave every check a `Params` model and made `scorers: default` follow the profile. The
form builds on both.

## Decision

### Two read-only endpoints; saving is the existing import

- **`GET /api/v1/scenarios/form`** (`FormOut`) returns what the form may offer, all of it from
  the server:
  - the profile's phases, its checkpoints (the commit gate flagged), its tools (committing ones
    flagged), whether it has traces, and `tool_calls_complete`;
  - the languages the simulator has a pack for, by each pack's own dialect name, never its
    aliases;
  - every registered check, with its area, what it reads (`chat`, `trace`, `frames`) and its
    settings;
  - the templates, and the default pack for a blank scenario;
  - whether the catalogue is read-only.
- **`POST /api/v1/scenarios/draft`** (`ScenarioDraftIn` → `DraftOut`) takes the form as filled
  in. It returns:
  - the YAML it becomes;
  - the folder and file name it would be saved as;
  - the loader's verdict (`ScenarioCheckOut`, the same answer `/scenarios/validate` gives the
    YAML editor);
  - what `scorers: default` would run for this scenario on this profile.

  It writes nothing, so it works on a read-only catalogue too.
- **Where a run may stop, and how a customer may answer, are the server's choices** too
  (`FormOut.stop_at`, `FormOut.answer_actions`):
  - Stop choices never include the irreversible outcome.
  - "decides from what is proposed" is offered only for a simulated customer, because the
    loader refuses it for a scripted one.
- **Every limit is said at a field, never as a bare 500.**
  - A request body the API's models refuse is a 422 `INVALID_REQUEST`, with a problem per
    field. This holds for every route, and the value sent is never echoed.
  - A check's settings are bounded to 8 levels and 20 KB before the YAML is written.
  - YAML over the editor's 200,000 characters is a problem in the verdict.
- **A folder the loader would never read is refused**, by the draft and by import alike:
  - an empty folder name;
  - `suites`, in any case. The loader skips it, and in a flat catalogue it is the suites
    folder itself.
- **Saving** sends the server's YAML, byte for byte, through `POST /scenarios/import`. That is
  the all-or-nothing path every new file already takes, and it never overwrites a file. The
  console then asks `GET /scenarios/{id}/source` where the file went, rather than assuming
  `scenarios/<folder>/`: a flat catalogue has no `scenarios/` folder.
  - The YAML editor had the same assumption. Both now share one helper
    (`api.saveNewScenario`), which says so if a file was written but cannot be found by its id.
  - A file named after a long id is shortened with a hash of the id, so two long ids never
    share a file.

### The YAML is ordinary, and the loader alone judges it

- `server/forms.py` builds the mapping in the order a person would write it, and leaves out
  whatever equals the model's default. The default turn limit and repeat count are read from
  `ConversationSpec` and `ExecutionSpec`, so they cannot drift from them.
- **`stop_at` is left out unless it is chosen**, so the profile decides where a run stops
  (ADR-0043). With a commit gate the run stops before the irreversible step; without one it runs
  to the goal.
- The dump is `SafeDumper`, with Unicode kept as text and aliases turned off. `opseval catalogue
  push` refuses anchors and aliases (ADR-0047), so the form must never write one. Text the user
  types comes back exactly as typed, whatever YAML it resembles (`key: value`, `!!python/…`,
  `yes`, `0123`).
- **What is saved is what was typed.** The YAML is read back and compared with what the form
  sent, and any field that differs is a problem at that field. YAML folds U+0085 (NEL) into a
  space, so a goal pasted with one would reach the judges changed.
- The file starts with the `yaml-language-server` schema hint, as `opseval init` writes it.
- **The draft model has no `allow_commit`.** A scenario that performs the irreversible outcome
  (ADR-0008) is written on purpose, in YAML. `stop_at: outcome_committed` in a draft is therefore
  a load problem, not a choice the form offers.
- The checks are not re-implemented. The YAML goes through `actions.check_scenario`, which runs
  `catalogue.check_scenario_text` (the schema, references, and params against each check's model)
  and then the console's own checks: an id clash, the folder and file name, and the warnings
  tier. A problem comes back at its YAML field path, for example `scorers.0.params.budjet`.

### Each check's settings, from its own `Params` model

`forms.param_fields` reads the model's fields:

- **Kind:** `text`, `integer`, `number`, `boolean`, `choice` (a `Literal`), `text_list`,
  `integer_list`, or `json` for anything else (`equals: Any`).
- **The rest:** whether the field is required, its bounds (`ge`/`gt`/`le`/`lt`), its default,
  and its description. `NoParams` now sets `use_attribute_docstrings`, so the description is the
  field's docstring, as it is for settings (ADR-0039).
- **A check without a `Params` model** gets `null`, never an empty list. A plugin that never
  declared its settings is "settings not described, set them in YAML", not "takes none".
- **A default the form cannot show** is shown as none: for example, a plugin factory that needs
  the other fields, or a value JSON cannot carry. A plugin model that cannot be read at all is
  "not described". Neither can take the form down, and the check still applies its own
  default.

The browser only decodes what was typed into a field: a number, a list, or JSON. An entry it
cannot decode is left out and marked under the field. Unquoted text in a JSON field is never
taken silently as a string. Whether a value is valid is the loader's answer.

### What is declared is graded

A form makes it easy to write down an expectation, so it must not make it easy to leave one
ungraded. A refusal or a rule that nothing reads looks checked, and an agent that breaks it
passes: a real failure hidden as a pass.

- **The default checks cover every declaration.** `scorers: default` now adds
  `judge.goal_achievement` for `expectations.kind: refusal` and `security.policy_adherence` for
  `expectations.policy` (`scoring.judged_expectations`). This amends ADR-0043, which already
  added a tool check for each tool rule.
  - These judges are added with or without a profile, so preflight's count of the LLM roles
    a suite needs includes them.
  - With no judge configured they are *not measured*, and named as such: a gap, never a
    silent pass.
  - A completion scenario without policy rules is unchanged and still runs in CI for free.
- **An explicit list that misses one is warned about.** `advice.scenario_warnings` names each
  declared expectation that no listed check grades: `must_call`, `must_not_call`,
  `forbidden_before`, `ordering`, a refusal, policy rules, and an `expected_outcome` with no
  judge. The warning shows in the form, the YAML editor and `opseval validate`. It is a
  warning, not an error, because a hand-written list is a deliberate choice.
- **Default checks plus others.** With the default checks on, a check the user adds is
  written out together with the pack for this draft. An added check's settings replace the
  pack's entry for it. Every template keeps the defaults and adds only what it is about, so a
  tool rule added to any template is graded.
- **A contradiction does not load.** A tool in both `must_call` and `must_not_call` fails
  whatever the agent does, so it is now a load error (`ExpectedTools`), wherever the scenario
  comes from.

### Templates are shapes, not content

The templates are domain-neutral starting points:

- a scripted question;
- a simulated customer graded by `judge.goal_achievement`;
- a refusal (`expectations.kind: refusal`);
- a prompt injection graded by `security.no_injection_takeover`;
- an approval step, offered only when the profile has checkpoints. It answers each checkpoint
  except the commit gate.

A test holds every template valid against the demo profile and against the profile
`opseval init` writes, which has no checkpoints, tools or phases. A team supplies its own
question, goal and rules. Clean-room policy: no scenario content is copied from anywhere.

### Save is enabled only by the verdict on what is in the form now

The form keeps each verdict with the exact draft it judged, as the YAML editor does with its
text. A late or out-of-order reply can therefore never vouch for different content.

Save needs three things:

- `ok` on the current draft;
- a writable catalogue;
- no field the browser could not decode.

A cleared number field keeps what was typed and holds Save until it reads as a number. Before,
it snapped to 1, so typing 5 gave 15.

### Where it sits in the console

- `/scenarios/new` is the form. `/scenarios/new/yaml` is the YAML editor for a new file, and
  `/scenarios/edit?file=` edits any file.
- The test-cases page offers both "New test case" (the form) and "Write YAML".

## Consequences

- A scenario can be added without writing YAML, and what is saved is a file a person can read,
  review in Git and edit by hand. No form-only format exists to migrate later.
- The form is storage-agnostic. When the console moves to the database catalogue (ADR-0047 step
  1), saving becomes "push a new version" in one place, and the draft endpoint is unchanged.
- Each new check describes its settings to the form by declaring a `Params` model with field
  docstrings. Nothing in the console changes for it.
- The console type generator now accepts an untyped field that has a default (`FormParam.default:
  unknown`). Before, it accepted one only without a default.
- **Not built yet:**
  - opening an existing scenario in the form (the YAML editor edits);
  - starting from a copy of a scenario or from a rehearsed conversation (D14);
  - injections planted in turns other than the first;
  - `sequence` answers and `via: user_message` rejects;
  - per-check `weight`;
  - the YAML-only fields (`timezone`, `consistency_threshold`, the attempt and turn timeouts,
    `forbidden_before` and `ordering` tool rules).

  The form writes ordinary YAML, so each of these can still be added in the YAML editor
  afterwards.
