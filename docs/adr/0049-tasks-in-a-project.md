# ADR-0049: Tasks in a project

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

Server mode ([ADR-0047](0047-server-mode-on-postgres.md)) follows Langfuse's tenancy:
organisation → project. The schema in `db/schema.py` has `organizations`, `projects` and
the versioned catalogue (`catalogue_items`, `catalogue_versions`, `catalogue_labels`). Every
catalogue item belongs directly to a project.

A project is the wrong unit for one evaluation target. A team that ships a chat agent, an
agentic back-office flow and a voice line inside one product wants them in one project, with
one set of members and keys. But each target has its own endpoint, protocol and modality, so
its own profile, scenarios, suites, runs and labels. Today the only way to separate them is a
second project, which splits the people and the keys too.

The owner asked for organisation → project → task, where a task is agentic, voice, and so on.

A catalogue folder already holds exactly one `profile.yaml` (`catalogue_store.read_folder`
refuses a folder without one). A folder is therefore already one evaluation target: a task
in all but name.

## Decision

**A task is a typed evaluation target inside a project.** It is one agent endpoint (its
profile) and one modality, together with that task's scenarios, suites, runs and labels.

**The table.**

```
tasks(id, project_id, slug, name,
      kind      chat | agentic | voice | batch,
      modality  text | audio_file | audio_stream | telephony | webrtc,
      profile_item_id, default_suite_item_id, region, archived_at, created_at)
unique (project_id, slug)
```

- `kind` says what the agent does; `modality` says how Opseval talks to it. Both are check
  constraints, so a typo fails in the database as it fails in the loader.
- `profile_item_id` points at the task's profile item. `default_suite_item_id` is the suite a
  run uses when it names none. Both are optional until the task has a catalogue.
- `region` is where the task's data may live. It matters once audio is stored
  ([ADR-0053](0053-voice-sessions-and-records.md)); for a text task it is informational.
- `archived_at` hides a task without deleting its runs. A task is never hard-deleted while a
  run points at it.

**Who owns what.**

- `catalogue_items` gains `task_id` (not null). The unique key moves from
  `(project_id, kind, name)` to `(task_id, kind, name)`, so two tasks may each have a scenario
  called `refund-happy-path`. `project_id` stays on the row for tenancy checks and indexes.
- `runs` ([ADR-0051](0051-runs-and-blobs-in-the-database.md)) carries `task_id` from the start.
- Labels stay per item, so `production` means "this task's production catalogue".
- A push and a pull lock the task row the way they lock the project row today, so a pull never
  mixes two pushes to the same task.

**Migration.** One Alembic revision:

1. creates `tasks`;
2. creates one task with slug `default` per existing project, kind `chat`, modality `text`;
3. sets `task_id` on every existing catalogue item to its project's `default` task, and links
   the task's profile if the project has exactly one;
4. makes `task_id` not null and swaps the unique key.

The test that compares migrations with models covers the new revision, as it covers the
others. The migration never guesses a kind or modality beyond the defaults; an operator
changes them afterwards.

**Naming a task.**

- CLI: `--task SLUG` on `catalogue push`, `catalogue pull`, `catalogue list`, `history`,
  `label`, and later `run --server`. Without it the `default` task is used, so every command
  written for ADR-0047 keeps working.
- API: task-scoped routes take the task slug in the path under the project. The console lists
  a project's tasks and opens one.
- A task that does not exist is an error naming the project and the slug, never an implicit
  create. `opseval db init --project org/p --task t` creates one explicitly.

**Local mode.** One catalogue folder is one task. File users see no change: `opseval run`,
`validate` and the console on `--catalogue` work as they do today, and the folder's
`profile.yaml` is the task's profile. A push of a folder goes into one task.

**No sharing across tasks, yet.** A scenario belongs to one task. A suite selects scenarios
of its own task only. Copying a scenario to another task is a new item there, with its own
version history.

## Consequences

- A project can hold a chat agent and a voice line with one membership list and one set of
  keys. Access can be narrowed per task through API keys ([ADR-0050](0050-accounts-and-access.md)).
- Analytics ([ADR-0052](0052-analytics-store.md)) and dashboards
  ([ADR-0066](0066-dashboards.md)) group by task as they group by suite, so a
  voice task's metrics never average into a chat task's pass rate.
- The unique-key change needs a data migration, not only a schema one. It is reversible while
  each project has only its `default` task.
- Every catalogue query gains a task filter. Forgetting it would leak one task's scenarios
  into another's run, so the store functions take a task, not a project, wherever they read
  items.

## Implementation (2026-10-28)

The database layer and its CLI are built; the console and runs are not.

- **Schema and migration.** `db.schema.Task` and revision `0002` (`0002_tasks.py`) as decided.
  Two details the decision left open:
  - `catalogue_items (task_id, project_id)` is a composite foreign key to `tasks (id,
    project_id)`, so an item can never sit in one project's row and another project's task.
  - `profile_item_id` and `default_suite_item_id` are composite foreign keys, `(item, task)`
    to `catalogue_items (id, task_id)`, so a task can only link its own items; a trigger
    (`opseval_task_link_kinds`) checks the profile is a `profile` and the suite a `suite`.
    They are `NO ACTION`, not `SET NULL`: Postgres before 15 cannot null one column of a
    composite key, and nothing deletes an item except with its task. A task cascades from its
    project, as items do; `runs.task_id` (ADR-0051) will be `RESTRICT`, which keeps a task
    with runs from being deleted.
  - The downgrade to `0001` works while every task is a `default` one as the upgrade made it
    (chat, text, no region, not archived, no default suite), and is refused otherwise, with a
    list of what it would lose.
- **A new project gets its `default` task** (`ensure_project`), so a project made after the
  migration looks like one made before it. Organisation, project and task are inserted
  `ON CONFLICT DO NOTHING` and then read, so two `db init` at once both succeed.
- **The store takes a task.** `push`, `set_label`, `list_items`, `history`, `resolve` and
  `materialize` take a `Task`, never a project. `get_task(session, "org/p", slug)` finds one.
  A missing task is a `ConfigError` naming `org/project`, the slug and the `db init --task`
  command, and nothing is created.
- **A push links the folder's profile** as the task's `profile_item_id`. If the profile's name
  changes, the link follows it to the new item.
- **The lock is the task row**: `FOR NO KEY UPDATE` for a push, a label move or a task update,
  `FOR SHARE` for a pull. An archived task refuses an update too (only `unarchive` changes it).
  Two tasks of one project never wait for each other.
- **Archiving.** An archived task refuses pushes and label moves. A write reads `archived_at`
  under its own lock, so an archive that commits first is always seen. Pull, list and history
  still work, so the catalogue can be taken out. Nothing is deleted.
- **CLI.**
  - `--task SLUG` (default `default`) on `catalogue push|pull|list|history|label`.
  - `db init --project org/p --task t [--kind --modality --region]` creates a task. On an
    existing task it reports "already exists", and refuses when the options asked for differ
    from the task, rather than ignore them.
  - `opseval task list [--all]`, `task update SLUG [--kind --modality --region --name
    --default-suite]`, and `task archive|unarchive SLUG` let an operator change the defaults
    the migration set.
- **Deferred.**
  - The task-scoped API routes and the console's task list are deferred to the console on the
    database catalogue (ADR-0047 step 1), because today's console reads only folders.
  - `run --server --task` is deferred to ADR-0051.
- **Tests.** `tests/test_db_tasks.py` covers:
  - the migration from `0001` with data (default tasks, moved items, the profile linked only
    when there is exactly one, the swapped key, a second upgrade, the downgrade and its
    refusal);
  - the check constraints and the composite key;
  - same-named items in two tasks;
  - `--task` on every command, and an unknown task (exit 3, nothing created);
  - archiving;
  - the per-task lock.

## Not decided here

- Sharing scenarios or suites across tasks (a library, or links between tasks).
- Whether a task may have more than one profile (for example staging and production
  endpoints). Today environments stay a profile concern.
- Moving a task between projects.
- How `kind` and `modality` change which scorers and dashboards are offered. The voice ADRs
  and [ADR-0066](0066-dashboards.md) decide that for their parts.
