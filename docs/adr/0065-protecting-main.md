# ADR-0065: Protecting `main` before and after the repository goes public

- **Status:** Proposed
- **Date:** 2026-10-28
- **Deciders:** Opseval maintainers (owner request 2026-10-28)

## Context

`main` has no protection. The repository is private, and on its current GitHub plan the API
answers HTTP 403 for both classic branch protection and rulesets on a private repository. The
owner will make the repository public at the end, after Gate 0; public repositories get
rulesets on every plan.

Meanwhile:

- CONTRIBUTING.md requires a DCO `Signed-off-by` line on every commit, but nothing checks it
  (ATTENTION I24).
- `.github/CODEOWNERS` names one personal account, so a required code-owner review would block
  every merge while there is a single maintainer.
- CI (`.github/workflows/ci.yml`) already has the jobs a merge should wait for: `check` (a
  Python 3.11–3.13 matrix), `console`, `container`, `audit` and `action`.

The aim is to get as close to real protection as the plan allows now, and to make the
go-public day one command, not a page of settings to remember.

## Decision

### Now: soft protection

- **A `dco` job in CI.** On every pull request, `scripts/check_dco.py` lists the PR's commits
  (`git log base..head`) and fails if any lacks a `Signed-off-by` line matching its author.
  It is a script in the repository, with no third-party app or token, so it runs the same in
  a fork. Merge commits made by GitHub are skipped.
- **A `pre-push` hook.** `scripts/githooks/pre-push` refuses a push whose target is
  `refs/heads/main`, and runs the fast checks (ruff and the schema drift check) before any
  other push. `make hooks` installs it by setting `core.hooksPath` to `scripts/githooks`. A
  `--no-verify` push still goes through: the hook is a guard against mistakes, not a control.
- **The ruleset, committed.** `.github/rulesets/main.json` is the ruleset GitHub will enforce:
  - pull request required, with one approving review from a code owner;
  - required status checks: the CI jobs above, `dco` included, by the names GitHub reports;
  - linear history (squash or rebase);
  - no force push, no deletion of `main`;
  - a bypass for repository admins, while there is a single maintainer.
- **`scripts/apply_rulesets.sh`.** It creates or updates the ruleset from the committed JSON
  through `gh api`. `--dry-run` prints the payload and the target without calling the API, so
  CI can check that the file is a valid payload today.

### On go-public day

`scripts/apply_rulesets.sh` is a step of the going-public checklist (ATTENTION I24), run right
after the visibility change. Once a maintainers team exists, CODEOWNERS names the team and the
admin bypass is removed from the ruleset in a normal PR.

## Consequences

- Unsigned commits are caught in review, which closes the DCO part of I24.
- Accidental direct pushes to `main` from a configured clone are refused locally.
- **Soft protection cannot stop a determined admin push.** Without server-side rules, an
  admin, or anyone with write access and `--no-verify`, can still push to `main`. The CI jobs
  run after the fact on `push`, so such a push is visible, but it is not prevented. That gap
  closes only when the ruleset is applied.
- The ruleset lives in the repository, so a change to the protection is a reviewed diff.
- The admin bypass means one maintainer can still merge without a second reviewer. It is
  named in the ruleset and in ATTENTION so it is not forgotten.
- Required check names must follow job renames. `apply_rulesets.sh --dry-run` in CI compares
  the required names with the jobs in `ci.yml` and fails on a mismatch.

## Not decided here

- Signed commits (GPG or SSH signatures) as a requirement; DCO sign-off is the contribution
  rule, not proof of identity.
- Protection for release tags (`v*`); `release.yml` already re-runs the checks, and a tag
  ruleset can be added on the same day.
- Moving to a paid plan to protect the private repository before then.
