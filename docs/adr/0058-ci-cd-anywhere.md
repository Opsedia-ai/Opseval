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

## Not decided here

- Where the image is published at go-public (GHCR or Docker Hub), and listing the GitLab component,
  CircleCI orb and Bitbucket pipe in their catalogues. Each needs a public namespace.
