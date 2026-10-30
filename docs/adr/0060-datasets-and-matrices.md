# ADR-0060: Datasets and matrices (flexibility level L1)

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)
- **Implementation:** Implemented (2026-10-28): `opseval.datasets` (parsing, placeholders,
  expansion), `dataset:`/`matrix:`/`expanded_from` on `Scenario`, the loader
  (`catalogue.check_expanded`, used by `load_catalogue` and `check_scenario_text`), the run
  snapshot and `rescore`, `catalogue push`/`pull` with migration `0004`, and the editor's check
  (`ScenarioCheckOut.expanded`). Details this ADR left open, decided as follows:
  - **Row numbers** are lines of the file: the CSV header is row 1, so the first data row is
    row 2, as a spreadsheet shows it; a JSONL row is its line. Blank lines are skipped and keep
    their numbers. CSV header names are trimmed; cells are kept exactly.
  - **JSONL** rows must all have the first row's keys; `null` is a blank; a duplicate key,
    `NaN`/`Infinity`, an array or an object is a load error. Numbers and booleans are written
    into text as JSON (`3`, `2.5`, `true`) and hashed with their type.
  - **Blank** means empty or whitespace only.
  - **The hash** is over `{"matrix": cell, "row": values}`; a persona variant is hashed by its
    content, not its index, so reordering personas keeps ids as reordering rows does. Two rows
    that expand to the *same scenario* (they differ only in columns no placeholder reads) are
    refused too, not only identical rows.
  - **Matrix axes.** `language` sets `persona.language` and drops the template's `dialect`
    (it belonged to the template's language) unless a `dialect` axis is present; `persona`
    variants override `role`, `typing_noise` and `timezone`, and a variant's role may use
    placeholders. A column named like a declared axis is
    refused. An `exclude` naming an axis the matrix lacks, or a value it does not list, is a
    load error, and excluding every cell is one too.
  - **Persona variants** may carry a `name` (`[A-Za-z0-9][A-Za-z0-9_.-]{0,39}`, unique).
    `{{persona}}` and `expanded_from.matrix.persona` are that name, else the first 8 hex
    digits of SHA-256 over the variant's content, never its index (reordering would change
    the text). `exclude` names a variant by index or name. A variant's `role` takes
    placeholders; its `name` and `timezone`, like any non-text field, may not.
  - **Languages** are checked per expansion by the existing reference check, so the ADR-0059
    rule holds unchanged: an unknown language is a load error for an LLM persona and a warning
    for scripted turns.
  - **Fields that cannot hold a placeholder** (tags, tool names, checkpoint names, …) refuse
    `{{` in any scenario; `\{{` is resolved in plain scenarios too.
  - **The catalogue root** for `datasets/` is decided once per load, from what was loaded,
    never per file or by a folder's name alone (`catalogue.dataset_root`): a suite's catalogue
    folder; for a folder or a single file (paths resolved first, so `validate .` inside
    `scenarios/` works), the nearest folder from there up holding `profile.yaml` or
    `datasets/`; without one, the loaded folder (a loaded `scenarios` folder's parent) or a
    file's own folder. The server, its editor and import pass the server's catalogue folder
    (`load_catalogue(root=…)`), so a flat catalogue, or a family folder named `scenarios`,
    reads the same rows everywhere.
  - **The cap is counted before anything is built**: the product of the axes is computed;
    with no `exclude` it is exact and refused at once. With `exclude`, cells are generated
    one at a time and counting stops one past the cap; a matrix over 100,000 cells before
    exclude (`datasets.MATRIX_SCAN_LIMIT`) is refused rather than scanned.
  - **Identity in the editor**: a template's file is known by its **authored** id (the
    server's index keeps authored and expanded ids apart). A rename is checked against
    suites selecting `id=<base>` and `id=<base>@<hash>`; an unchanged template is never a
    rename. Import refuses an authored id a dataset scenario holds. The index's cache
    signature includes `datasets/**`, so a new row is seen.
  - **Selection.** An authored id selects every expansion in `--filter id=`, a suite's
    `filters` and the console's run-these-scenarios list (`RunOptions.only_ids`). Duplicate ids
    are checked on the authored id. Tags and criticality are inherited.
  - **Snapshots** are named by id with `@` kept (`storage.SAFE_ID`), so an expansion never
    shares a file with an authored `a_1a2b3c4d`. No layout bump: an older reader refuses a
    snapshot with `expanded_from` (`extra="forbid"`) rather than misreading it.
  - **Settings** are read only when a scenario expands, and only the two limits
    (`config.CatalogueLimits`, `Limits.from_env`), so a broken unrelated setting never stops a
    load or `validate`; a broken limit is a load problem naming `OPSEVAL_MAX_…`.
  - **Field paths**: a reserved `@` is reported at `id`; `dataset:` is decided by shape (a
    string is a path, a mapping a spec), so a bad path is one problem at `dataset.path`.
  - **Audio folders** use `storage.SAFE_ID` like snapshots and records (`@` kept).
  - **Push** names a dangling or unreadable file (no traceback), and refuses an item name over
    200 characters (`db.schema.ITEM_NAME_MAX`) or a path over 500 before the transaction.
  - **Migration `0004`** widens `ck_catalogue_items_kind`. `0001` created it as
    `ck_catalogue_items_ck_catalogue_items_kind` (the naming convention applied twice); `0004`
    drops either name and creates the models' `ck_catalogue_items_kind`, and its downgrade
    restores the released name. The downgrade refuses while any dataset item is stored.
  - **Not built yet:** provenance in the HTML report and the console's run detail (the id
    carries the base and hash; `expanded_from` is in the snapshot), and analytics grouped by
    base id, language or persona (ADR-0052).

## Context

Today a scenario is one conversation. A team that wants the same refund flow for twenty order types,
or in Egyptian, Gulf and MSA Arabic, copies the file twenty times. The copies drift, and a fix lands
in some of them. This is level L0 of the flexibility ladder (YAML and the guided form, ADR-0048). L1
lets one scenario stand for many, without giving up the rules that make a scenario safe to trust:

- authoring mistakes fail at load time ([ADR-0043](0043-defaults-from-the-profile.md));
- `rescore` grades exactly what ran ([ADR-0013](0013-offline-rescore.md));
- a gap in coverage is never silent.

A template engine such as Jinja would give loops, conditionals and filters. It would also move
authoring errors to run time and put expression evaluation on text read from data files.

## Decision

### `dataset:` and `{{var}}`

```yaml
id: refund-by-order-type
dataset: datasets/orders.csv      # or .jsonl
goal: Get a refund for order {{order_id}} ({{order_type}}).
conversation:
  opening_message: Hi, I want my money back for {{order_id}}.
```

- **Formats.** CSV with a header row, or JSONL with one object per line. Values are scalars (string,
  number, boolean). A nested value is a load error.
- **Where the file may be.** One place: under `datasets/` at the catalogue root, beside
  `profile.yaml`, `suites/` and `scenarios/`. The path is relative to the catalogue root, not to
  the scenario file, so a scenario moved between folders keeps working. Any other path, `..`, or
  a link pointing outside the folder is a load error.
- **What the catalogue store needs** ([ADR-0047](0047-server-mode-on-postgres.md),
  `db/catalogue_store.py`):
  - `catalogue push` stores `datasets/**/*.csv` and `datasets/**/*.jsonl`: `_files` lists them
    beside the YAML files, with the same refusal of links that leave the folder;
  - `catalogue_items.kind` gains `dataset` (the check constraint in `db/schema.py`), named by its
    path, and versioned and labelled like the other kinds;
  - pull's `_safe_target` allows those two extensions, only under `datasets/` (today it allows
    `.yaml` only, under `suites/` and `scenarios/` or as `profile.yaml`);
  - a size cap per dataset file, `OPSEVAL_MAX_DATASET_BYTES` (default 5 MB), checked at load and
    at push, in place of the 256 KB `MAX_FILE_BYTES` that YAML files keep; over it is an error
    naming the file, never a truncation.
- **Column names** match `[A-Za-z_][A-Za-z0-9_]*`. A duplicate column is a load error.
- **Placeholders are logic-free.** `{{name}}` (spaces inside allowed) is replaced by the value.
  There are no filters, conditionals, loops, defaults or expressions. `\{{` writes a literal `{{`.
- **Substitution happens on the parsed model, never on the YAML text.** Values replace placeholders
  inside string fields only (goal, name, persona role, opening message, flow, scripted turns,
  injections, checkpoint feedback, expectations, string scorer params). A value holding `:` or a
  newline cannot change the file's structure. `id`, `schema`, scorer ids and numbers cannot hold
  placeholders.
- **Values are not re-scanned.** A row value containing `{{x}}` stays literal, so data cannot pull
  in other data.
- **Mistakes fail at load:**
  - a placeholder with no column or matrix key is a load error, naming the file, row and field;
  - `{{…}}` in a scenario without `dataset:` or `matrix:` is a load error (it is a typo, not text);
  - a blank cell is a load error unless the column is listed in `dataset.allow_blank`;
  - two rows that expand to the same scenario are a load error;
  - an expanded scenario that fails validation is reported with its row number;
  - a column no placeholder uses is a warning in `opseval validate` (`advice.scenario_warnings`).

### `matrix:`

```yaml
matrix:
  language: [ar-EG, ar-SA, en-US]
  persona:
    - { role: an impatient customer, typing_noise: 0.2 }
    - { role: a first-time customer }
  exclude:
    - { language: en-US, persona: 0 }
```

- **Axes.** `language` (BCP-47, resolved by the language packs of
  [ADR-0059](0059-languages-and-dialects.md), so an unknown language is a load error, never
  English), `dialect` (for the old aliases), and `persona` (overrides of the scenario's persona
  fields).
- **The product.** The expansion is every dataset row × every matrix cell, less `exclude`.
- **Placeholders.** Matrix keys are placeholders too, so `{{language}}` works in text.

### Expansion: at load, with a cap

- The loader expands. The runner, scorers, verdicts and reports see ordinary scenarios.
- **The cap.** `OPSEVAL_MAX_EXPANSION` (default 100) applies per scenario. Going over it is a load
  error that names the count. It is never a truncation, because a truncated set is coverage dropped
  silently. `opseval validate` shows each scenario's expansion count and its attempts (count ×
  repeats).
- **Stable ids.** Each expansion's id is `<id>@<hash>`. The hash is the first 8 hex digits of
  SHA-256 over the canonical JSON of the row's values and the matrix cell, with sorted keys.
  - Reordering rows keeps every id.
  - Changing a value gives a new id, so the trend view (FR-RPT-06) never compares two different
    conversations under one name.
  - `@` is reserved: an authored id can never contain it.
- **Filters.** `--filter id=refund-by-order-type` selects every expansion, and
  `id=refund-by-order-type@1a2b3c4d` selects one.
- **Provenance.** Each expanded scenario carries `expanded_from` (base id, dataset path and digest,
  row number, matrix cell), shown in the report and the console.

### Snapshots and rescore

- The run's snapshot keeps the **expanded** scenarios, keyed by expanded id, plus the dataset
  digests. `load_snapshot` already returns a `dict[str, Scenario]`.
- `opseval rescore` grades from the snapshot. A dataset edited after the run cannot change what a
  saved run is graded against.
- Judges' rubric digests
  ([ADR-0017](0017-degraded-visibility-rubric-digests-and-ballot-independence.md)) are computed on
  the expanded text, so a score graded for one row is never reused for another.
- The JSON Schemas are regenerated (`opseval schemas`, [ADR-0026](0026-published-json-schemas.md)).

## Consequences

- One file, many verdicts. Each expansion is its own scenario in the gate, so one failing row fails
  the gate as any scenario would, and is never averaged away.
- Analytics ([ADR-0052](0052-analytics-store.md)) can group results by base id, language, dialect
  and persona.
- Runs get bigger. The cap and the attempt count in `validate` make the cost visible before a run.
- The guided form (ADR-0048) shows a preview of the expansion from a server endpoint that uses the
  loader. The browser expands nothing.
- Teams who need logic write a script that produces the CSV. Opseval's files stay data.

## Not decided here

- Remote datasets (a URL, a Langfuse dataset). They would need allowlisting and a digest pinned in
  the catalogue version.
- Sampling a subset of a large dataset per run. It would need a recorded seed, so a run can be
  repeated.
