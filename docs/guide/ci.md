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
`artifact-name` and `github-token`. Its outputs are `exit-code`, `gate` (`pass` or `block`) and
`run-dir`. Settings such as the judge's endpoint and key go in `env:` as `OPSEVAL_*` variables
(see [Settings](../reference/settings.md)). The PR comment needs `pull-requests: write`.

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
      allowed-hosts: agent.staging.example.com
    secrets:
      judge-api-key: ${{ secrets.OPSEVAL_JUDGE_API_KEY }}
  release:
    needs: eval                   # skipped on exit 1, 2 or 3
```

- Inputs: the action's (except `github-token`), plus `allowed-hosts` (`OPSEVAL_ALLOWED_HOSTS`)
  and `opseval-ref`.
- Secrets: `simulator-api-key`, `judge-api-key` and `agent-token` (`OPSEVAL_STATIC_TOKEN`), all
  optional.
- Outputs: `exit-code` and `gate`. If the action never ran to the end, `exit-code` is `3`.
- It declares no permissions of its own. A called workflow can only lower the caller's token, so
  the calling job grants `contents: read`, plus `pull-requests: write` for the PR comment.

### After a deployment

`deployment-status.yml` runs only when `github.event.deployment_status.state == 'success'`. The
deployment's `environment_url` reaches the script through `env:`, never pasted into it. It must
be `https://`, and its host must already be in `OPSEVAL_ALLOWED_HOSTS`. A deployment to a host
nobody allowlisted is exit 3, never a run.

## Other CI systems

Run the CLI (or the container image) and keep the exit code:

```bash
opseval run evals/suites/smoke.yaml --profile evals/profile.yaml \
  --target wss://agent.staging.example.com/ws --junit junit.xml
opseval summary reports/<run>     # the same Markdown gate summary, for a job summary
```

Templates for GitLab, Jenkins, Azure DevOps, CircleCI and Bitbucket are planned
([ADR-0058](../adr/0058-ci-cd-anywhere.md), E3).
