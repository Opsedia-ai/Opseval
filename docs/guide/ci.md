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
`artifact-name`, `read-dotenv` and `github-token`. Its outputs are `exit-code`, `gate` (`pass` or
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

- Inputs: the action's (except `github-token`), plus `allowed-hosts` (`OPSEVAL_ALLOWED_HOSTS`),
  `opseval-ref`, `simulator-base-url`, `simulator-model`, `judge-base-url`, `judge-model` and
  `opseval-env`. A caller's `env:` does not reach a called workflow, so settings come in as inputs.
- `opseval-ref` must be a full 40-character commit SHA; a branch, a tag or a short SHA is exit 3.
- `opseval-env` takes one `OPSEVAL_NAME=value` per line (blank lines and `#` comments are
  skipped). A secret setting (pass it through `secrets:`), a setting that has its own input, a
  duplicate, or a line of any other shape is exit 3, and then nothing is set.
- A simulator or judge key without both its base URL and its model is exit 3: nothing would use
  it.
- Secrets: `simulator-api-key`, `judge-api-key` and `agent-token` (`OPSEVAL_STATIC_TOKEN`), all
  optional.
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
  `OPSEVAL_SIMULATOR_API_KEY`, `OPSEVAL_JUDGE_API_KEY` and `OPSEVAL_STATIC_TOKEN`. A secret is
  never an input, and `opseval-env` refuses a secret setting with exit 3;
- fails on 1, 2 and 3, and names which one it was.

All of them run one script, `templates/shared/opseval-ci.sh`; each template carries a copy and a
test keeps the copies identical. Two details hold everywhere:

- `opseval-env` follows the reusable workflow's rules: one `OPSEVAL_NAME=value` per line; a
  secret setting, a setting with its own input, a duplicate or a line of another shape is exit 3
  and nothing runs. A simulator or judge key with no base URL and model is exit 3.
- Exit 2 with no `result.json` is Opseval refusing its arguments (a typo in `extra-args`), so it
  is reported as 3, a configuration error. Any code other than 0 to 3 (Docker could not start
  the image, say) is reported as 3.

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
- `opsevalEnv` takes a map or a string of lines; `filter` a list or a string of lines.
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

- Keys are secret variables (or a variable group linked to Key Vault) named
  `OPSEVAL_SIMULATOR_API_KEY`, `OPSEVAL_JUDGE_API_KEY` and `OPSEVAL_STATIC_TOKEN`. Azure never
  puts a secret into a script's environment by itself, so the template maps exactly those
  three. An undefined one counts as no key.
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
- The image has no git, so `checkout` uses CircleCI's built-in git client.
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
- The other variables are the inputs in upper case (`SUITE`, `EXTRA_ARGS`, `OPSEVAL_ENV`, ...).
- The pipe keeps the image's non-root user. If the clone is not writable by it, the pipe
  stops with exit 3 and says so.

## Any other CI: `docker run`

Run the image with the workspace mounted, keep the exit code, and publish
`opseval-reports/junit.xml`. Keys come from the CI system's secret store into the job's
environment, and `-e NAME` (with no value) passes them on by name, so they never appear in a
command line. `OPSEVAL_DOTENV=false` keeps a `.env` in the checkout from configuring the run.

```bash
cat > opseval.env <<'EOF'
OPSEVAL_DOTENV=false
OPSEVAL_ALLOWED_HOSTS=agent.staging.example.com,api.openai.com
OPSEVAL_JUDGE_BASE_URL=https://api.openai.com/v1
OPSEVAL_JUDGE_MODEL=gpt-4.1-mini
EOF
mkdir -p opseval-reports
code=0
docker run --rm --user "$(id -u):$(id -g)" -e HOME=/tmp \
  -v "$PWD:/work" -w /work --env-file opseval.env -e OPSEVAL_JUDGE_API_KEY \
  opseval run evals/suites/smoke.yaml --profile evals/profile.yaml \
  --target wss://agent.staging.example.com/ws \
  --reports-dir opseval-reports --junit opseval-reports/junit.xml || code=$?
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
New-Item -ItemType Directory -Force opseval-reports | Out-Null
docker run --rm -e HOME=/tmp -v "${PWD}:/work" -w /work --env-file opseval.env `
  -e OPSEVAL_JUDGE_API_KEY `
  opseval run evals/suites/smoke.yaml --profile evals/profile.yaml `
  --target wss://agent.staging.example.com/ws `
  --reports-dir opseval-reports --junit opseval-reports/junit.xml
$code = $LASTEXITCODE
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
