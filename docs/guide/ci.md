# Gate releases in CI

Opseval's exit code is the release decision. Every CI integration passes it through unchanged:

| Code | Meaning | Blocks a release |
|---|---|---|
| 0 | pass | no |
| 1 | the agent failed | yes |
| 2 | not judged | yes, and named as "not judged", not as an agent failure |
| 3 | harness or configuration error | yes, and named as a harness error |

Nothing here offers a switch that turns 2 or 3 into a pass, and none should be added around it
(`continue-on-error`, `|| true`, `if: always()` on the job that ships). A suite that judged
nothing must not ship ([ADR-0032](../adr/0032-gate-policy.md)).

## GitHub Actions: drop a file in `.github/workflows/`

Copy one of the files in `examples/github/` of the Opseval repository into your repository's
`.github/workflows/`, then change the suite, the profile, the target and the allowlisted hosts:

| File | Runs on | What it does |
|---|---|---|
| `minimal.yml` | every pull request | the action and nothing else |
| `pull_request.yml` | every pull request | simulator and judge keys from secrets, the gate summary as a PR comment |
| `tag-gate.yml` | a pushed `v*` tag | the reusable workflow; the release job runs only on exit 0 |
| `release-published.yml` | a published release | evaluates the released tag, then a promotion job that needs it |
| `deployment-status.yml` | a successful deployment | evaluates the URL that was just deployed |
| `report-to-server.yml` | a push to `main` | runs the server's `production` label and reports the run back ([below](#report-runs-to-an-opseval-server)) |

Each one uploads `report.html`, `junit.xml` and `result.json` as an artifact and writes the gate
summary to the job summary. Make the job a required check to block a merge.

!!! note "Before go-public"
    The examples use `opsedia-ai/opseval-action@v1` for readability. That repository is published
    at go-public (Gate 0). Until then use `Opsedia-ai/Opseval@<full commit SHA>`, the action at
    the root of the main repository. In production, pin a full commit SHA rather than a tag.

### The action

```yaml
- uses: opsedia-ai/opseval-action@v1
  with:
    suite: evals/suites/smoke.yaml
    profile: evals/profile.yaml
    target: wss://agent.staging.example.com/ws
  env: { OPSEVAL_ALLOWED_HOSTS: agent.staging.example.com }
```

Its inputs are `suite`, `profile`, `target`, `transport`, `repeats`, `filter`, `extra-args`,
`python-version`, `opseval-spec` (a pip requirement such as `opseval==0.2.0`), `comment`,
`artifact-name`, `read-dotenv`, `github-token`, and, to report the run to a server,
`server-url`, `project`, `task`, `label`, `opseval-public-key` and `opseval-secret-key`
(`profile` is then optional: the label has one). Its outputs are `exit-code`, `gate` (`pass` or
`block`) and `run-dir`. Settings such as the judge's endpoint and key go in `env:` as `OPSEVAL_*`
variables (see [Settings](../reference/settings.md)). The PR comment needs `pull-requests: write`.

The checked-out repository cannot configure the run. Opseval is installed into a virtualenv in
`$RUNNER_TEMP` with no uv configuration from the workspace (`UV_NO_CONFIG=1`, no cache), and it
reads no `.env` from the workspace (`OPSEVAL_DOTENV=false`) unless you set `read-dotenv: "true"`.
A failure while writing the job summary or the PR comment is a warning, never the result: the
last step always exits with Opseval's own code, and with 3 if Opseval never finished.

On a pull request from a fork, GitHub passes repository variables but no secrets.
`pull_request.yml` therefore sets each LLM endpoint only when its key is there:

- with neither endpoint nor key, nothing is sent to an LLM: scenarios with an LLM persona are
  skipped and judge checks are not measured, so the run is exit 2 (not judged), or 1 if a
  scripted scenario still fails a deterministic check;
- with the endpoint and model set but the key missing, every LLM request would be refused:
  exit 3, a harness error.

### The reusable workflow

`.github/workflows/opseval-eval.yml` wraps the action in a job, so another job can depend on it
with `needs:`. It runs only when called (`on: workflow_call`).

```yaml
jobs:
  eval:
    permissions: { contents: read }
    uses: Opsedia-ai/Opseval/.github/workflows/opseval-eval.yml@<sha>
    with:
      opseval-ref: <sha>          # the same commit: the action it runs
      suite: evals/suites/release.yaml
      profile: evals/profile.yaml
      target: wss://agent.staging.example.com/ws
      allowed-hosts: agent.staging.example.com,api.openai.com
      judge-base-url: https://api.openai.com/v1
      judge-model: gpt-4.1-mini
      opseval-env: |             # any other non-secret setting, one per line
        OPSEVAL_SIMULATOR_TIMEOUT_SECONDS=120
    secrets:
      judge-api-key: ${{ secrets.OPSEVAL_JUDGE_API_KEY }}
  release:
    needs: eval                   # skipped on exit 1, 2 or 3
```

- Inputs: the action's (except `github-token` and the server key, a secret here), plus `allowed-hosts` (`OPSEVAL_ALLOWED_HOSTS`),
  `opseval-ref`, `simulator-base-url`, `simulator-model`, `judge-base-url`, `judge-model` and
  `opseval-env`. A caller's `env:` does not reach a called workflow, so settings come in as inputs.
- `opseval-ref` must be a full 40-character commit SHA; a branch, a tag or a short SHA is exit 3.
- `opseval-env` takes one `OPSEVAL_NAME=value` per line (blank lines and `#` comments are
  skipped). A secret setting (pass it through `secrets:`), a setting that has its own input, a
  duplicate, or a line of any other shape is exit 3, and then nothing is set.
- A simulator or judge key without both its base URL and its model is exit 3: nothing would use
  it.
- Secrets: `simulator-api-key`, `judge-api-key` and `agent-token` (`OPSEVAL_STATIC_TOKEN`), and
  the server key `opseval-public-key` and `opseval-secret-key`, all optional.
- Outputs: `exit-code` and `gate`. If the action never ran to the end, `exit-code` is `3`.
- It declares no permissions of its own. A called workflow can only lower the caller's token, so
  the calling job grants `contents: read`, plus `pull-requests: write` for the PR comment.

### After a deployment

`deployment-status.yml` runs only when `github.event.deployment_status.state == 'success'`. The
deployment's `environment_url` reaches the script through `env:`, never pasted into it. It must
be `https://`, and its host must be in the repository variable `OPSEVAL_AGENT_HOSTS`: the hosts
agents are deployed to, kept apart from `OPSEVAL_ALLOWED_HOSTS` so that allowlisting an LLM
endpoint never makes it a deployment target. Unset, or a host not in it, is exit 3, never a run.

A `deployment_status` run has the repository's secrets, while the deployed commit may be anyone's
(a preview of a fork's pull request). So the example runs only for one named environment that
trusted refs alone deploy to, checks the deployed commit out into `deployed/` for its scenarios
and profile, and never installs or runs anything from it.

## Report runs to an Opseval server

With an Opseval server (`opseval serve` in accounts mode, on Postgres), a CI job can run what a
catalogue label holds and send the run back, where it is listed with every other run
([ADR-0058](../adr/0058-ci-cd-anywhere.md) E2):

```bash
export OPSEVAL_ALLOWED_HOSTS=opseval.example.com,agent.staging.example.com
export OPSEVAL_PUBLIC_KEY=pk-oe-…        # a project API key (ADR-0050)
export OPSEVAL_SECRET_KEY=sk-oe-…        # from the CI system's secret store
opseval run suites/smoke.yaml --target wss://agent.staging.example.com/ws \
  --server https://opseval.example.com --project acme/support-bot --task default --label production
```

1. **Pull.** The label's files (suite, scenarios, profile, datasets) come over the server's API
   (`GET /api/v1/catalogue/export`, scope `runs:read`) into a new temporary folder, with the
   same rules as `opseval catalogue pull`: every stored path is checked again before anything is
   written. The path argument names a file inside it (`suites/smoke.yaml`), and the label's
   `profile.yaml` is the profile: `--profile` is refused when the label has one. Neither the
   database nor the `server` extra is needed, so the CLI image can do this.
2. **Run.** Locally, exactly as without `--server`, into `OPSEVAL_REPORTS_DIR`. The commit guard
   still needs `--allow-commit` on the command line; a label never grants it.
3. **Upload.** The finished run folder (manifest, records, result, report, JUnit, the profile and
   scenarios as run, sealed audio with each file's sha256) goes to `PUT
   /api/v1/runs/{run_id}/upload` (scope `runs:start`), with the catalogue versions that were
   pulled and where the CI job ran (provider, repository, ref, commit, a link to the job, read
   from GitHub Actions, GitLab, Jenkins, Azure DevOps, CircleCI or Bitbucket variables).

**The exit code is always the local verdict's.**

- A pull that fails (the server unreachable, a wrong key, a label nothing carries, a stored path
  that is not a catalogue file) is **exit 3**: nothing was evaluated.
- An upload that fails never changes the code. It is tried again on a connection error, a
  timeout, 408, 425, 429, any 5xx, or 409 `RUN_IMPORTING` (another try of the same run is
  still being stored) (`OPSEVAL_SERVER_UPLOAD_RETRIES`, waiting 2 s, 4 s, 8 s, …, at most 30 s
  or the server's `Retry-After`), never on another refusal (401, 403, 404, 409, 413, 422). The
  outcome is an `opseval:server` entry in `result.sinks` (a name reserved for Opseval, so a
  plugin sink called `server` keeps its own), so a failure is named in the console summary
  (`NOT PUBLISHED opseval:server: …`), the gate summary (`opseval summary`: **Not published**) and the
  JUnit file (`failed_sinks` property). The run folder stays; `opseval runs import` can send it
  later.
- The upload is idempotent by run id: sending the same run again is answered `unchanged`, so a
  try whose answer was lost is safe. A different run under an id the server holds is refused
  (409), never written over. Two uploads of one run at once are serialised: the second is
  answered 409 `RUN_IMPORTING` and tries again, and never removes the first while it is
  being stored.

The server checks the upload before writing anything: its size (`OPSEVAL_UPLOAD_MAX_MB`, 413),
its layout (409 for one newer than the server's, before the rest is read), every field with the
run models (`extra="forbid"`, 422), and each audio file's sha256 (422). It writes the run through
the run store into the served project and task, exactly as `opseval runs import` does, and
records an audit event `run.upload`. A key scoped to another task, or a request for a project or
task the server does not serve, gets 404. The console lists the run like any other, with a
**CI** badge and a link to the job.

The key: the server URL is allowlisted (https unless loopback) before the key is sent anywhere;
the key goes only in the `Authorization` header, never in a log line, a report or an error, and
`OPSEVAL_SECRET_KEY` is redacted in every config digest.

In GitHub Actions, give the action `server-url`, `project` (and `task`, `label`) with the key as
`opseval-public-key` / `opseval-secret-key` (`examples/github/report-to-server.yml`); the
reusable workflow takes the same inputs and the key as `secrets:`. Every template under
`templates/` takes `server-url`, `project`, `task` and `label` in its own spelling, and the key
from the system's secret store as `OPSEVAL_PUBLIC_KEY` and `OPSEVAL_SECRET_KEY` (Jenkins:
`opsevalPublicKeyCredentialsId`/`opsevalSecretKeyCredentialsId`; CircleCI: the
`opseval_public_key`/`opseval_secret_key` variable names; Bitbucket: `PUBLIC_KEY`/`SECRET_KEY`).

## Other CI systems

Templates for GitLab, Jenkins, Azure DevOps, CircleCI and Bitbucket are in `templates/` of the
Opseval repository ([ADR-0058](../adr/0058-ci-cd-anywhere.md), E3). Each one:

- runs the CLI container image ([ADR-0038](../adr/0038-container-image.md)) as a non-root user,
  with `--junit`, and publishes the JUnit file and the run folder even when the job fails;
- takes the inputs the GitHub action takes (`suite`, `profile`, `target`, `transport`,
  `repeats`, `filter`, `extra-args`, `read-dotenv`) and the reusable workflow's settings
  (`allowed-hosts`, `simulator-base-url`, `simulator-model`, `judge-base-url`, `judge-model`,
  `opseval-env`), spelled the way each system spells names, plus `image`;
- takes secrets only from the system's own secret store, under Opseval's names
  `OPSEVAL_SIMULATOR_API_KEY`, `OPSEVAL_JUDGE_API_KEY` and `OPSEVAL_STATIC_TOKEN`, and any other
  secret setting named in `secret-env-names`. A secret is never an input, and `opseval-env`
  refuses a secret setting with exit 3;
- fails on 1, 2 and 3, and names which one it was.

All of them run one script, `templates/shared/opseval-ci.sh`; each template carries a copy and a
test keeps the copies identical. These details hold everywhere:

- `opseval-env` follows the reusable workflow's rules: one `OPSEVAL_NAME=value` per line; a
  secret setting, a setting with its own input, a duplicate or a line of another shape is exit 3
  and nothing runs. A simulator or judge key with no base URL and model is exit 3.
- Exit 0, 1 and 2 count only when the run left a `result.json` that records the same code.
  Otherwise nothing was judged: Opseval refused its arguments (a typo in `extra-args`), or the
  image's entrypoint is not `opseval`. That is reported as 3, a configuration error. Any code
  other than 0 to 3 (Docker could not start the image, say) is reported as 3 too.
- `extra-args` may not hold `--help` or `--version` (Opseval would print and exit, judging
  nothing), nor `--reports-dir` or `--junit` (the template sets them): exit 3 before anything
  runs.
- Each build writes its run to a folder of its own, `opseval-reports/build-XXXXXX/`, and first
  removes an earlier build's folder. It removes only folders it made itself and marked with a
  `.opseval-ci-build` file, never a link and never anything else. So a reused workspace (a
  Jenkins agent, a self-hosted Azure agent) publishes this build's run and no other.
- `secret-env-names` passes other secret settings, such as `OPSEVAL_LANGFUSE_SECRET_KEY` or
  `OPSEVAL_WEBHOOK_URL`, by name only. Each must be a secret setting (`config.SECRET_FIELDS`),
  or it is exit 3. Put the value in the system's secret store under that name. In the image
  (GitLab, CircleCI, Bitbucket) the job's environment already reaches Opseval. With
  `docker run` (Jenkins, Azure DevOps) each one is passed as `-e NAME`, never as a value on the
  command line or in the env file.
- With `docker run`, the workspace is mounted at its own absolute path (`-v "$PWD:$PWD" -w
  "$PWD"`), so absolute paths inside it work. A suite, profile or reports folder outside the
  workspace is exit 3, since the container cannot see it. The container runs as the job's own
  uid:gid. A job running as root runs it as the image's user 10001 instead, and gives that user
  this build's run folder (`chown`). No folder is ever made world-writable.
- On Azure DevOps (`TF_BUILD=True`) Opseval's output passes through a filter: a `##vso[` or
  `##[` in it, which could come from the agent's own words, gets a zero-width space after the
  first `#`, so the agent cannot run logging commands such as `task.complete`.

!!! warning "Docker on the host, from inside a container"
    `docker run -v` mounts a path on the Docker *host*. When the CI agent itself runs in a
    container that talks to the host's Docker (a mounted Docker socket: a Jenkins agent in
    Docker, an Azure DevOps container job), the workspace's path inside the agent is not a
    path on the host, and the mount is empty or wrong. Set `docker-workspace-host-path`
    (`dockerWorkspaceHostPath`) to where the workspace is on the host. The run then fails
    (exit 3) rather than judging nothing if the path is wrong, because no `result.json` comes
    back.

!!! note "Before go-public"
    The image is published at go-public (Gate 0). Until then every template's `image` defaults
    to the placeholder `ghcr.io/opsedia-ai/opseval:<version>`, which fails with exit 3 and how to
    fix it. Build the image from the Opseval repository with `docker build -t opseval .`, push it
    to a registry your CI can pull from, and set `image`. The GitLab component, the CircleCI orb
    and the Bitbucket pipe are listed in their catalogues at go-public too.

The templates are checked by tests (each file parses, the shared script is run with a fake
`opseval` and a fake `docker` for exit codes 0 to 3), not by running GitLab, Jenkins, Azure
DevOps, CircleCI or Bitbucket.

## GitLab

`templates/gitlab/opseval.yml` is a CI/CD component (`spec: inputs:`): a job in the Opseval
image, with `artifacts: reports: junit` and the run folder as an artifact (`when: always`).

```yaml
stages: [test, release]

include:
  - project: opsedia-ai/opseval            # your mirror, until the catalog component exists
    ref: <full commit SHA>
    file: templates/gitlab/opseval.yml
    inputs:
      suite: evals/suites/smoke.yaml
      profile: evals/profile.yaml
      target: wss://agent.staging.example.com/ws
      allowed-hosts: agent.staging.example.com,api.openai.com
      judge-base-url: https://api.openai.com/v1
      judge-model: gpt-4.1-mini
      image: registry.example.com/tools/opseval:0.2.0

release:
  stage: release
  needs: [opseval]                         # runs only when Opseval exited 0
  script: [./scripts/publish.sh]
```

- Keys are masked (and preferably hidden and protected) CI/CD variables named
  `OPSEVAL_JUDGE_API_KEY` and so on; GitLab hands them to the job.
- The job runs in the image, so other `OPSEVAL_*` CI/CD variables reach Opseval too.
- Extra inputs: `stage`, `job-name` (include the file twice to run two suites) and
  `artifact-name`.
- `filter` takes one filter; pass more as `extra-args: --filter tag=a --filter tag=b`.
- Never add `allow_failure` to the job, least of all `allow_failure: exit_codes: [2, 3]`.
- Every input reaches the script as a variable with `expand: false`, so a `$` in an input stays
  a `$` and never reads another CI/CD variable.
- `secret-env-names: OPSEVAL_LANGFUSE_SECRET_KEY` names another secret setting held as a masked
  CI/CD variable.
- A catalog component must be `templates/<name>.yml` in its own project, so this file is
  included with `include: project:` until it is published as `opsedia-ai/opseval/opseval`.
- The image runs as uid 10001. GitLab's Docker executor checks the project out
  world-writable by default; on a runner that does not, the job stops with exit 3 and says so.

## Jenkins

`templates/jenkins/` is a shared library. Register it as `opseval` (library path
`templates/jenkins/`, pinned to a commit), and the step `opseval(...)`
(`templates/jenkins/vars/opseval.groovy`) runs the image with `docker run` on an agent that
has Docker. An example is `templates/jenkins/Jenkinsfile.example`.

```groovy
@Library('opseval') _
pipeline {
    agent { label 'docker' }
    stages {
        stage('Evaluate') {
            steps {
                opseval(
                    suite: 'evals/suites/smoke.yaml',
                    profile: 'evals/profile.yaml',
                    target: 'wss://agent.staging.example.com/ws',
                    allowedHosts: 'agent.staging.example.com,api.openai.com',
                    judgeBaseUrl: 'https://api.openai.com/v1',
                    judgeModel: 'gpt-4.1-mini',
                    judgeApiKeyCredentialsId: 'opseval-judge-api-key',
                    image: 'opseval',
                )
            }
        }
        stage('Release') { steps { sh './scripts/publish.sh' } }
    }
}
```

- Keys are Secret text credentials, named by id: `simulatorApiKeyCredentialsId`,
  `judgeApiKeyCredentialsId`, `agentTokenCredentialsId`. The step binds them with
  `withCredentials`, and they reach the container by name, never on a command line.
- Any other secret setting is a Secret text credential too, in
  `secretCredentials: [OPSEVAL_LANGFUSE_SECRET_KEY: 'langfuse-secret']` (setting name to
  credential id). A name that is not a secret setting is exit 3.
- `opsevalEnv` takes a map or a string of lines; `filter` a list or a string of lines.
- An agent in a container that uses the host's Docker needs `dockerWorkspaceHostPath` (see the
  warning above).
- It publishes `junit` with `skipMarkingBuildUnstable: true` (the build's result is the exit
  code, not the test count) and archives the run folder.
- Exit 1, 2 and 3 are each `FAILURE`, with a message naming which one, never `UNSTABLE`: an
  unstable build still lets later stages and promotions run.
- An unknown parameter fails the step.
- The container runs as the agent's own user (never root), so it can write the reports into the
  workspace.

## Azure DevOps

`templates/azure/opseval.yml` is a steps template with `parameters:`. It runs the image with
`docker run` (Microsoft-hosted Ubuntu agents have Docker), then runs `PublishTestResults@2`,
uploads the gate summary to the run's summary page and publishes the run folder, all with
`condition: succeededOrFailed()`.

```yaml
resources:
  repositories:
    - repository: opseval
      type: github
      endpoint: github-opseval
      name: Opsedia-ai/Opseval
      ref: <full commit SHA>

variables:
  - group: opseval-secrets                  # OPSEVAL_JUDGE_API_KEY, marked secret

steps:
  - checkout: self
  - template: templates/azure/opseval.yml@opseval
    parameters:
      suite: evals/suites/smoke.yaml
      profile: evals/profile.yaml
      target: wss://agent.staging.example.com/ws
      allowedHosts: agent.staging.example.com,api.openai.com
      judgeBaseUrl: https://api.openai.com/v1
      judgeModel: gpt-4.1-mini
      image: myregistry.azurecr.io/opseval:0.2.0
```

- Keys are secret variables named `OPSEVAL_SIMULATOR_API_KEY`, `OPSEVAL_JUDGE_API_KEY` and
  `OPSEVAL_STATIC_TOKEN`. Azure never puts a secret into a script's environment by itself, so
  the template maps exactly those three, and the names in `secretEnvNames`
  (`secretEnvNames: [OPSEVAL_LANGFUSE_SECRET_KEY]`).
- A variable group linked to Key Vault works too. Key Vault secret names cannot hold `_`, so
  name the secret `OPSEVAL-JUDGE-API-KEY`: the template reads both spellings, and two different
  values for one key is exit 3.
- An undefined variable reaches the step as its own macro text, `$(OPSEVAL_JUDGE_API_KEY)`.
  The template treats that as no value. When the simulator's or judges' base URL and model are
  set but their key is undefined, that is exit 3 naming the key, not a run whose judges cannot
  authenticate. Set `llmWithoutKey: true` for an endpoint that takes no key.
- Opseval's output is filtered so that no line of it can be a `##vso[` logging command.
- `failTaskOnFailedTests` is off: the gate decides, not the test count.
- Never set `continueOnError` on the step.
- `templates/azure/azure-pipelines.example.yml` gates a `Release` stage on it.

## CircleCI

`templates/circleci/orb/orb.yml` is an orb. Its `run` job checks the project out and runs the
suite on the `default` executor, the Opseval image. The `evaluate` command does the same inside
a job of your own on that executor. Both use `store_test_results` and `store_artifacts`.

```yaml
version: 2.1
orbs:
  opseval: opsedia-ai/opseval@1.0.0          # until published: paste the orb inline
workflows:
  release:
    jobs:
      - opseval/run:
          context: opseval-secrets           # OPSEVAL_JUDGE_API_KEY
          image: registry.example.com/tools/opseval:0.2.0
          suite: evals/suites/smoke.yaml
          profile: evals/profile.yaml
          target: wss://agent.staging.example.com/ws
          allowed_hosts: agent.staging.example.com,api.openai.com
          judge_base_url: https://api.openai.com/v1
          judge_model: gpt-4.1-mini
      - deploy:
          requires: [opseval/run]            # runs only when Opseval exited 0
```

- Keys come from a context or project environment variables. The parameters
  `simulator_api_key`, `judge_api_key` and `agent_token` are `env_var_name`s (by default
  Opseval's own names), so a value never appears in the expanded config.
- `secret_env_names` names other secret settings (held in the context under their own names).
- The image has no git, so `checkout` uses CircleCI's built-in git client.
- The executor sets neither `entrypoint` nor `command`. CircleCI then ignores the image's
  `ENTRYPOINT` (`opseval`) for the primary container and runs the steps in it. Setting
  `entrypoint` would make CircleCI run it instead.
- `filter` takes one filter; pass more through `extra_args`.

## Bitbucket

`templates/bitbucket/pipe/` is a pipe: `pipe.yml`, a `Dockerfile` built `FROM` the Opseval
image, and its entrypoint `templates/bitbucket/pipe/pipe.sh`. The pipe exits with Opseval's
code and writes `test-results/opseval/junit.xml`, which Bitbucket shows as test results. The
run folder goes to `opseval-reports/`.

```yaml
pipelines:
  pull-requests:
    "**":
      - step:
          name: Opseval
          script:
            - pipe: docker://registry.example.com/tools/opseval-pipe:0.2.0
              variables:
                SUITE: evals/suites/smoke.yaml
                PROFILE: evals/profile.yaml
                TARGET: wss://agent.staging.example.com/ws
                ALLOWED_HOSTS: agent.staging.example.com,api.openai.com
                JUDGE_BASE_URL: https://api.openai.com/v1
                JUDGE_MODEL: gpt-4.1-mini
                JUDGE_API_KEY: $OPSEVAL_JUDGE_API_KEY   # a secured variable
          artifacts:
            - opseval-reports/**
```

- Build the pipe until it is published:
  `docker build -t opseval-pipe --build-arg OPSEVAL_IMAGE=opseval templates/bitbucket/pipe`.
- Keys are secured repository or deployment variables, passed to the pipe as
  `SIMULATOR_API_KEY`, `JUDGE_API_KEY` and `AGENT_TOKEN`. Only those set a key.
- Another secret setting is passed under its own name (`OPSEVAL_LANGFUSE_SECRET_KEY:
  $OPSEVAL_LANGFUSE_SECRET_KEY`) and listed in `SECRET_ENV_NAMES`.
- The other variables are the inputs in upper case (`SUITE`, `EXTRA_ARGS`, `OPSEVAL_ENV`, ...).
- Bitbucket mounts the clone owned by root, which the image's user 10001 cannot write. So the
  pipe starts as root, makes `opseval-reports/` and `test-results/opseval/`, gives both to
  10001, and runs everything else as 10001 with `setpriv`. Opseval itself never runs as root,
  and no folder is made world-writable. The pipe's image build runs `setpriv --version`, so an
  image without it fails to build.

## Any other CI: `docker run`

Run the image with the workspace mounted at its own path, keep the exit code, and publish
`opseval-reports/junit.xml`. Keys come from the CI system's secret store into the job's
environment, and `-e NAME` (with no value) passes them on by name, so they never appear in a
command line. `OPSEVAL_DOTENV=false` keeps a `.env` in the checkout from configuring the run.
Trust 0, 1 and 2 only when the run left a `result.json`: without one nothing was judged. On a
reused workspace, write each build to a fresh folder (here `mktemp -d`) and publish only it.

```bash
cat > opseval.env <<'EOF'
OPSEVAL_DOTENV=false
OPSEVAL_ALLOWED_HOSTS=agent.staging.example.com,api.openai.com
OPSEVAL_JUDGE_BASE_URL=https://api.openai.com/v1
OPSEVAL_JUDGE_MODEL=gpt-4.1-mini
EOF
mkdir -p opseval-reports
out=$(mktemp -d opseval-reports/build-XXXXXX)
code=0
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:$PWD" -w "$PWD" --env-file opseval.env -e OPSEVAL_JUDGE_API_KEY \
  opseval run evals/suites/smoke.yaml --profile evals/profile.yaml \
  --target wss://agent.staging.example.com/ws \
  --reports-dir "$out" --junit opseval-reports/junit.xml || code=$?
if [ "$code" -le 2 ] && ! ls "$out"/*/result.json > /dev/null 2>&1; then
  echo "Opseval: exit $code but no result.json: nothing was judged"; code=3
fi
case "$code" in
  0) echo "Opseval: pass" ;;
  1) echo "Opseval: the agent failed" ;;
  2) echo "Opseval: not judged (blocks the release)" ;;
  3) echo "Opseval: harness or configuration error (blocks the release)" ;;
  *) echo "Opseval did not finish ($code): a harness error"; code=3 ;;
esac
exit "$code"
```

The same in PowerShell, on a Windows agent running Linux containers:

```powershell
@"
OPSEVAL_DOTENV=false
OPSEVAL_ALLOWED_HOSTS=agent.staging.example.com,api.openai.com
OPSEVAL_JUDGE_BASE_URL=https://api.openai.com/v1
OPSEVAL_JUDGE_MODEL=gpt-4.1-mini
"@ | Set-Content -Encoding ascii opseval.env
$out = "opseval-reports/build-$([guid]::NewGuid().ToString('N'))"
New-Item -ItemType Directory -Force $out | Out-Null
docker run --rm -e HOME=/tmp -v "${PWD}:/work" -w /work --env-file opseval.env `
  -e OPSEVAL_JUDGE_API_KEY `
  opseval run evals/suites/smoke.yaml --profile evals/profile.yaml `
  --target wss://agent.staging.example.com/ws `
  --reports-dir $out --junit opseval-reports/junit.xml
$code = $LASTEXITCODE
if ($code -le 2 -and -not (Get-ChildItem "$out/*/result.json" -ErrorAction SilentlyContinue)) {
  Write-Host "Opseval: exit $code but no result.json: nothing was judged"; $code = 3
}
switch ($code) {
  0 { Write-Host "Opseval: pass" }
  1 { Write-Host "Opseval: the agent failed" }
  2 { Write-Host "Opseval: not judged (blocks the release)" }
  3 { Write-Host "Opseval: harness or configuration error (blocks the release)" }
  default { Write-Host "Opseval did not finish ($code): a harness error"; $code = 3 }
}
exit $code
```

`opseval summary opseval-reports/<run>` (run the same way) prints the Markdown gate summary for
a job summary. Without Docker, `pip install opseval` and run the same `opseval run` line.
