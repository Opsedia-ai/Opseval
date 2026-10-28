# Contributing to Opseval

Thank you for helping build Opseval! This guide explains how to propose changes and get them merged.

> Opseval is **pre-alpha**. The code skeleton lands in the first sprint; until then, the most useful contributions are feedback on the [roadmap](ROADMAP.md) and the [architecture decisions](docs/adr/).

## Ways to contribute

- **Report a bug or request a feature.** Use the issue templates.
- **Propose a larger change** to the scenario schema, run record, score states, exit codes, plugin APIs or REST API. Open an **RFC** in GitHub Discussions (category *RFCs*). After a 7-day comment period, the decision is recorded as an ADR in `docs/adr/`.
- **Build a plugin**: a language pack, a SUT profile for an agent framework, a scorer, a transport or a trace source. Plugins are the best first contribution.
- **Improve the docs and examples.**

## Development setup (from Sprint 1)

```bash
git clone https://github.com/Opsedia-ai/Opseval.git
cd Opseval
uv sync            # installs the workspace with dev dependencies
make hooks         # the pre-push hook: refuses a push to main, runs ruff and the schema check
make check         # ruff, import-linter, bandit, mypy --strict, pytest (coverage >= 85%)
make audit         # needs network: pip-audit and licence check of runtime dependencies
make demo          # runs the example refund agent + smoke suite
```

Requirements: Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). You need Node 22.22+ or 24.15+ only for the web console (see `engines` in `packages/console/package.json`).

## Pull request checklist

- [ ] The PR is focused on one thing and links an issue (or explains why none is needed).
- [ ] Tests are added or updated. Coverage does not drop.
- [ ] `make check` passes locally (ruff, import-linter, bandit, mypy strict, pytest).
- [ ] **Harness faults map to `infra_error`.** Any new failure mode that comes from the tool itself, not the agent, must never produce `failed`.
- [ ] Docs are updated for user-facing changes.
- [ ] `docs/STATUS.md` is updated if the PR changes what Opseval can do (then run `uv run python scripts/project_status.py`).
- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` …).
- [ ] Every commit is signed off (see DCO below).

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA. Add a sign-off line to each commit:

```bash
git commit -s -m "feat(scoring): add tools.args_valid scorer"
# adds: Signed-off-by: Your Name <you@example.com>
```

By signing off, you certify that you wrote the contribution, or otherwise have the right to submit it under the Apache-2.0 licence.

The sign-off must carry the email your commits are authored with (`git config user.email`); the name may differ. The `dco` CI job (`scripts/check_dco.py`) checks every commit of a pull request, and only those (commits of `main` you merged into your branch are not yours to sign), and fails the pull request if one is not signed off. A merge commit with no changes of its own (GitHub's "Update branch", a clean `git merge main`) is skipped, whoever made it; a merge that changes anything (a conflict you resolved, an edit made while merging) needs a sign-off too: `git merge --signoff`, or `git commit -s` after resolving. To sign off commits already on your branch:

```bash
git rebase --signoff origin/main   # adds the line to every commit since main
git push --force-with-lease
```

Dependabot signs off with its own address, which the check accepts. You can run the check yourself: `python3 scripts/check_dco.py origin/main HEAD`.

## `main` is protected

Changes reach `main` only through a pull request ([ADR-0065](docs/adr/0065-protecting-main.md)). `make hooks` sets `core.hooksPath` to `scripts/githooks` in your clone, so a `git push` to `main` of `Opsedia-ai/Opseval` is refused locally (pushing `main` to your fork is fine), and any other push first runs ruff and the schema drift check (`SKIP_PRE_PUSH_CHECKS=1` skips those checks, never the refusal). Setting `core.hooksPath` switches off the hooks in `.git/hooks`, so `make hooks` refuses when you have hooks of your own there or `core.hooksPath` is already set; move them to `scripts/githooks` or run `make hooks FORCE=1`. The hook guards against mistakes; it is not a control.

On GitHub, `main` is protected by the ruleset in `.github/rulesets/main.json`: a pull request with an approving code-owner review, every CI job passing (`dco` included, each reported by GitHub Actions), linear history (squash or rebase), and no force push or deletion. Repository admins may merge their own pull request without the review (a bypass in `pull_request` mode, needed while there is a single code owner, who cannot approve their own pull request); nobody can push to `main` directly. GitHub refuses rulesets on the repository while it is private, so the maintainers apply it with `scripts/apply_rulesets.sh --apply` when it goes public; until then the dry run in CI keeps it in step with `ci.yml`. A change to the protection is a change to that file, reviewed like any other. If you rename or add a CI job, update the ruleset in the same pull request (`bash scripts/apply_rulesets.sh` tells you what is out of step). A CI job may not have a job-level `if:`: GitHub reports a skipped job as a passing check, so put conditions on its steps.

**Clean contributions only.** Do not submit code, prompts, rubrics or test data copied from an employer's or client's private systems, or from any source whose licence isn't compatible with Apache-2.0.

## The `ee/` directory

Code under `ee/` is covered by the commercial Opseval Enterprise License and is written by the Opsedia team. We don't accept community pull requests into `ee/`. If you think an `ee/` capability belongs in the open-source core, open a Discussion. Our [governance pledge](GOVERNANCE.md) is that evaluation features are never moved behind the licence key.

## Code style

- Python: ruff (lint and format), mypy `--strict`, Pydantic v2 models with `extra="forbid"`.
- Every source file carries an SPDX header: `# SPDX-License-Identifier: Apache-2.0` (checked by a test).
- Runtime dependencies of the core must use a permissive licence: MIT, BSD, Apache-2.0, ISC, PSF or MPL-2.0. GPL-family, SSPL, BSL, ELv2 and non-commercial licences are not accepted (`make audit` checks this).
- Error messages must tell the user what to do next.
- Never show an unmeasured value as `0`. Use `None` and render "not measured".

## Getting help

- Questions: GitHub Discussions (Q&A).
- Chat: the community Discord (link coming with the public launch).
- Security issues: see [SECURITY.md](SECURITY.md). Do not open a public issue.

Everyone in the community is expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).
