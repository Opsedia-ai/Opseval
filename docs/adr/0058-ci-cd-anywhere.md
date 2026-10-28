# ADR-0058: CI/CD anywhere: a published GitHub Action, server reporting, templates for other CI systems

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

[ADR-0015](0015-ci-integrations.md) gave Opseval a composite action (`action.yml` at the repository
root), `opseval summary` and `pytest-opseval`. The CI job `action` dogfoods it against the demo
agent, and the job `container` checks the four demo exit codes inside the image
([ADR-0038](0038-container-image.md)). What is missing:

- **A drop-in for GitHub.** The action is reachable only as `Opsedia-ai/Opseval@<sha>`. The
  Marketplace lists only a public repository holding the action alone, with no workflow files.
- **Reporting to a server.** [ADR-0047](0047-server-mode-on-postgres.md) plans `opseval run --server
  …` for CI, but not what happens when the server cannot be reached.
- **Other CI systems** get only the cookbook's `docker run` line: no exit codes, no JUnit wiring.

The one rule holds in CI too: a harness problem must never look like an agent failure, and a real
agent failure must never be hidden as "not measured". Exit codes 2 and 3 are never turned green.

## Decision

### E1: GitHub, drag and drop

- **Where the action lives.** Its source moves to `integrations/github-action/action.yml`.
  - The root `action.yml` stays until go-public, so `Opsedia-ai/Opseval@<sha>` users are not broken.
    A test keeps the two identical, apart from where Opseval is installed from.
  - Inside the monorepo, an empty `opseval-spec` keeps installing from the action's own checkout.
- **Publishing.** `release.yml` gains a job, after `verify`, `build` and `publish`, that copies the
  action to `opsedia-ai/opseval-action`. It moves the `v1` tag and writes a full `vX.Y.Z` tag.
  - The copy sets `opseval-spec`'s default to `opseval==X.Y.Z`, because that repository has no
    `packages/` to install from. The action and the CLI therefore always share a version.
  - That repository holds only `action.yml`, `README.md` and `LICENSE`. It is published at go-public
    (Gate 0), not before; until then the job stays off.
- **A reusable workflow.** `.github/workflows/opseval-eval.yml` has `on: workflow_call` only.
  - It lives in the main repository, because the action repository may hold no workflow files.
  - Inputs mirror the action's, plus `allowed-hosts`; secrets are the LLM keys and the agent token;
    outputs are `exit-code` and `gate`. As in ADR-0015, inputs reach scripts only via `env:`.
- **Examples.** `examples/github/` holds one workflow per trigger: `pull_request` (with the PR
  comment), `push` with `tags: ["v*"]` (the tag gate below), `release` with `types: [published]`,
  and `deployment_status`. They pin `@v1` for readability, with a comment to pin a full SHA.
- **After a deployment.** The `deployment_status` example runs only when
  `github.event.deployment_status.state == 'success'`, with the target from
  `github.event.deployment_status.environment_url`. That URL reaches the step through `env:`, and
  its host must already be in `OPSEVAL_ALLOWED_HOSTS`: a deployment to a host nobody allowlisted is
  exit 3, never a run.

The minimal snippet, then the tag gate (nothing is published unless the evaluation exits 0):

```yaml
- uses: opsedia-ai/opseval-action@v1
  with:
    suite: evals/suites/smoke.yaml
    profile: evals/profile.yaml
    target: wss://agent.staging.example.com/ws
  env: { OPSEVAL_ALLOWED_HOSTS: agent.staging.example.com }
```

```yaml
on:
  push:
    tags: ["v*"]
jobs:
  eval:
    uses: Opsedia-ai/Opseval/.github/workflows/opseval-eval.yml@<sha>
    with:
      suite: evals/suites/release.yaml
      profile: evals/profile.yaml
      target: wss://agent.staging.example.com/ws
      allowed-hosts: agent.staging.example.com
    secrets:
      simulator-api-key: ${{ secrets.OPSEVAL_SIMULATOR_API_KEY }}
      judge-api-key: ${{ secrets.OPSEVAL_JUDGE_API_KEY }}
  release:
    needs: eval          # skipped on exit 1, 2 or 3
    runs-on: ubuntu-latest
    steps:
      - run: ./scripts/publish.sh
```

### E2: report runs to the server

`opseval run --server URL --project org/p --task t --label production`:

- **Keys.** `OPSEVAL_PUBLIC_KEY` and `OPSEVAL_SECRET_KEY`. The secret key is a `SecretStr` in
  `SECRET_FIELDS`, so `config_digest` redacts it. The key's scopes and tasks come from
  [ADR-0050](0050-accounts-and-access.md).
- **The server URL** goes through `check_url_allowed` (allowlisted, TLS unless loopback). A refused
  host is exit 3, before anything runs.
- **Pull.** The labelled catalogue is fetched over the server's HTTP API and written with the same
  rules as `opseval catalogue pull`: a new or empty folder only, stored paths checked again. The CLI
  and the container image therefore need neither `OPSEVAL_DATABASE_URL` nor the `server` extra. A
  failed pull is exit 3, since nothing was evaluated.
- **Run.** Local and unchanged. The commit guard ([ADR-0008](0008-committed-outcome-guard.md)) still
  needs `--allow-commit` on the command line; a label can never grant it.
- **Upload.** The finished run folder goes up, with the catalogue versions that were pulled.
- **The exit code always comes from the local verdict.** An upload failure is reported like a sink
  failure ([ADR-0027](0027-sinks.md)): an entry in `result.sinks`, named in the console output, the
  gate summary and the job log. It never changes or hides the exit code. The run folder stays, and
  `opseval runs import` ([ADR-0051](0051-runs-and-blobs-in-the-database.md)) can send it later.

### E3: templates for other CI systems

Each template wraps the CLI image (ADR-0038), runs as its non-root user, passes `--junit`, and
publishes the JUnit file and the run folder even when the step fails:

| System | Form |
|---|---|
| GitLab | a CI/CD component with `spec:inputs`, JUnit through `artifacts:reports:junit` |
| Jenkins | a shared library step `opsevalRun(...)` and an example `Jenkinsfile` (`junit` step) |
| Azure DevOps | a YAML template with `parameters:`, `PublishTestResults@2` |
| CircleCI | an orb with a `run` job, `store_test_results` |
| Bitbucket | a pipe, with test reports from the mounted folder |
| anything else | a documented `docker run` line |

Each one maps the exit codes the same way:

| Code | Meaning | Blocks a release |
|---|---|---|
| 0 | pass | no |
| 1 | the agent failed | yes |
| 2 | not judged | yes, and named as "not judged", not as an agent failure |
| 3 | harness or configuration error | yes, and named as a harness error |

No template offers a switch that turns 2 or 3 into a pass (such as GitLab's `allow_failure:
exit_codes`): [ADR-0032](0032-gate-policy.md) says nothing judged blocks and exit 3 stays 3. The
`opseval summary` Markdown goes to the system's job summary where one exists.

### Verification

- The `action` CI job calls the reusable workflow on the demo agent: exit 0 healthy, exit 1 with
  `--bug skip-approval`. The dogfood steps use `./integrations/github-action`.
- A matrix job validates each template with its own tool where one runs in CI (`gitlab-ci-local`,
  `circleci config validate`), and the rest by schema. Each template is then run against the image
  where the system can run in CI, checking the four demo exit codes, 0, 1, 1 and 3.
- E2 fault-injection tests: a refused pull is exit 3; a failed upload leaves a passing run at 0 and
  a failing run at 1, with the failure in `result.sinks`.

## Consequences

- One action contract in three places (monorepo, action repo, reusable workflow), one drift test.
- Azure DevOps and Bitbucket templates cannot run in our CI: schema-checked only, as STATUS says.
- `--server` adds a network dependency to CI, which cannot change a verdict.

## Implementation notes

**E1, 2026-10-28.** Built as decided, with these details:

- `integrations/github-action/action.yml` is the source; the root `action.yml` is a byte-for-byte
  copy (tested). The install step finds Opseval next to whichever copy runs
  (`$ACTION_PATH/packages/opseval`, else `$ACTION_PATH/../../packages/opseval`, else exit 3 asking
  for `opseval-spec`).
- The reusable workflow cannot take the action from its own commit: `uses:` takes no
  expressions, and no context names the called workflow's SHA. It therefore checks Opseval out at
  the input `opseval-ref` (the same SHA the caller pins) into `.opseval-action/` and runs the action
  from there. `opseval-ref` must be a full 40-character lowercase SHA (a branch, a tag or a short
  SHA is exit 3). An empty one is accepted only when `github.repository` is `Opsedia-ai/Opseval`,
  and then the workspace's own `integrations/github-action` runs with no second clone.
- It declares no permissions. A called workflow can only lower the caller's token, so declaring
  `pull-requests: write` would make every caller without it (a tag gate) fail. Each caller grants
  what its use needs, and the examples grant `pull-requests: write` only on `pull_request`.
- `exit-code` is `3` when the action never ran to the end (for example a failed checkout), as the
  action itself reports a missing code.

**Review of #80, 2026-10-28.**

- Settings for a called workflow. A caller's `env:` never reaches a called workflow, so the
  reusable workflow takes `simulator-base-url`, `simulator-model`, `judge-base-url` and
  `judge-model`, plus `opseval-env`: one `OPSEVAL_NAME=value` per line for any other non-secret
  setting. A line must match `^OPSEVAL_[A-Z0-9_]+=`; a secret setting (every name in
  `config.SECRET_FIELDS`, kept equal by a test), a setting with its own input, a duplicate or a
  carriage return is exit 3, and nothing is written to `$GITHUB_ENV` unless every line passes.
  Values arrive through `env:` and are split on newlines, so a value can never add a line. Keys
  stay `secrets:`. A simulator or judge key with no base URL and model is exit 3: the run would
  never use it, and the caller would believe the judges ran.
- The install ignores the workspace. `setup-uv` runs in `$RUNNER_TEMP` with its cache off, and
  `uv venv`/`uv pip install` run there with `UV_NO_CONFIG=1`, so a checked-out repository's
  `uv.toml`, `pyproject.toml` or `.python-version` cannot choose the index, the Python or the cache
  Opseval is installed from. Opseval itself reads no workspace `.env` in the action: a new setting,
  `OPSEVAL_DOTENV=false` (read from the real environment only; a `.env` cannot switch itself off),
  stops `Settings` and `secret_named` from reading one, and the action sets it unless the input
  `read-dotenv: "true"`. Running from another directory was the alternative; it would have changed
  what every relative path in `extra-args`, a profile or a setting means.
- Reporting never decides. The summary and comment step runs with `set +e` and turns every
  failure (the summary, listing comments, posting) into a warning; the exit step is
  `if: always()` and re-exits with the recorded code, so 1, 2 and 3 stay failures and a missing
  code is 3.
- `pull_request.yml` sets each LLM endpoint only when its key is there. A fork PR has repository
  variables but no secrets: with the endpoint and no key every LLM request would be refused
  (exit 3); with neither, LLM personas are skipped and judges unmeasured (exit 2, or 1 if a
  scripted scenario fails).
- `deployment-status.yml` checks the deployed host against `OPSEVAL_AGENT_HOSTS`, a repository
  variable of its own (unset is exit 3), so allowlisting an LLM endpoint never makes it a valid
  deployment target. It runs only for one named environment that trusted refs alone deploy to, and
  checks the deployed commit out into `deployed/` as data (scenarios and profile) only.
- A job that calls a workflow has no steps, so the `action` job in `ci.yml` cannot call it, and a
  failing called workflow fails the whole run (no `continue-on-error` on such a job). The
  reusable workflow is dogfooded in its own file, `reusable-workflow.yml`, on the healthy demo
  agent only (exit 0, gate `pass`), started by the boolean input `start-demo-agent` (installed once
  into the venv the action then reuses); exit 1 is
  proven on the action, whose `action` job now runs the source on the healthy agent and the root
  copy on the buggy one. Keeping it out of `ci.yml` keeps that file's jobs the required checks.
- The `deployment_status` example checks the URL itself before anything is installed (https,
  host in `OPSEVAL_AGENT_HOSTS`, else exit 3), on top of Opseval's own refusal; the check is tested
  by running its script.
- Not built: the `release.yml` job that publishes `opsedia-ai/opseval-action` (off until Gate 0,
  so it is left for go-public rather than added disabled), and the action repository's README.
  No `actionlint` run: the tests parse and check the files offline.

**E3, 2026-10-28.** Built in `templates/`, with these details:

- **One script.** Every template runs `templates/shared/opseval-ci.sh`: it validates the
  settings, builds the `opseval run` line, runs it, copies the JUnit file, writes
  `summary.md` and exits with the code.
  - GitLab, CircleCI and Azure DevOps inline a copy, the Jenkins library ships one as a resource,
    and the Bitbucket pipe uses one as its entrypoint. `scripts/ci_templates.py` rewrites the
    copies, and a test keeps them identical.
  - The text has no heredoc and no `<<` (CircleCI reads it as a parameter) and no `$(name)` with
    no space (Azure DevOps expands it as a macro), so the same bytes survive every system.
  - Inputs reach it only through the environment, never pasted into the script.
- **Two modes.** `MODE=local` runs `opseval` from PATH: GitLab and CircleCI run the job in the
  image, and the pipe is built from it. `MODE=docker` runs `docker run` for Jenkins, Azure DevOps
  and the documented line.
  - With Docker, the container gets only the validated settings (an env file outside the
    workspace) and the three keys by name (`-e NAME`, never a value on the command line).
  - It runs as the job's own uid:gid so it can write the reports into the mounted workspace,
    and as the image's user 10001 when the job runs as root. It never runs as root.
- **The E1 rules, once.** Inputs mirror the action's and the reusable workflow's settings
  inputs, in each system's spelling. `opseval-env` refuses a secret setting (the list is tested
  equal to `config.SECRET_FIELDS`), a setting with its own input, a duplicate or another shape,
  and a simulator or judge key with no base URL and model is exit 3. Before anything runs,
  `OPSEVAL_DOTENV` is `false` unless `read-dotenv` is true.
- **Exit codes.** 0 to 3 pass through unchanged. Exit 2 with no `result.json` is Click refusing
  its arguments (a typo in `extra-args`), reported as 3, a configuration error, not as "not
  judged". Any other code (Docker could not start the image, 125) is 3.
  - A stale `junit.xml` or `summary.md` from an earlier build in a reused workspace is deleted
    first.
  - Jenkins publishes `junit` with `skipMarkingBuildUnstable: true`, and 1, 2 and 3 are each
    `FAILURE` with their own message, never `UNSTABLE`.
  - Azure DevOps sets `failTaskOnFailedTests: false`. Its three publishing steps run on
    `succeededOrFailed()` and cannot turn the failed step green.
  - A test refuses `allow_failure`, `continueOnError`, `continue-on-error`, `catchError`,
    `unstable(`, `|| true` and `set +e` in any template.
- **Secrets** come from each system's own store, under Opseval's names: GitLab masked CI/CD
  variables, Jenkins Secret text credentials bound by `withCredentials`, Azure secret variables
  mapped to `env:`, CircleCI contexts named by `env_var_name` parameters, Bitbucket secured
  variables passed to the pipe.
  - On Azure an undefined secret reaches the step as its own macro text; the template treats
    that as no key.
- **Per system.**
  - The Jenkins step is `opseval(...)` (`vars/opseval.groovy`), not `opsevalRun`: a library's
    step is named after its file.
  - The CircleCI orb has the `run` job and an `evaluate` command, since `run` is a built-in step.
  - The Bitbucket pipe writes JUnit to `test-results/opseval/junit.xml`, where Bitbucket looks,
    and keeps the run folder in `opseval-reports/` so the run's own `junit.xml` is not counted
    twice.
  - A GitLab catalog component must be `templates/<name>.yml` in its own project, so until it is
    published the file is included with `include: project:` and the same inputs.
- **The image.** It is not published before Gate 0, so every `image` input defaults to
  `ghcr.io/opsedia-ai/opseval:<version>`. With Docker that placeholder is exit 3, saying how to
  build the image (`docker build -t opseval .`).
- **Not built.** The verification matrix job. `ci.yml`'s jobs must equal the required checks
  (ADR-0065), `gitlab-ci-local` and the `circleci` CLI are not installed, and Jenkins, Azure
  DevOps and Bitbucket cannot run in GitHub Actions. So the templates are checked by pytest
  only (`tests/test_ci_templates.py`):
  - every file parses, and the shell scripts pass `bash -n`;
  - the shared script runs under `bash -e` with a fake `opseval` and a fake `docker`, for exit
    codes 0 to 3, the E1 settings rules and secrets by name;
  - the Bitbucket, Azure and CircleCI preludes run the same way.

  Publishing the image, the GitLab component, the orb and the pipe waits for go-public.

## Not decided here

- Where the image is published at go-public (GHCR or Docker Hub), and listing the GitLab component,
  CircleCI orb and Bitbucket pipe in their catalogues. Each needs a public namespace.
