# ADR-0015: CI integrations: a composite GitHub Action and a pytest plugin

- **Status:** Proposed
- **Date:** 2026-09-24
- **Deciders:** Opseval maintainers

## Context

Opseval's value is a release gate teams trust (PRD UJ-03, FR-GATE-03/04). Most teams gate in GitHub Actions, and many already run pytest in CI. Both integrations must keep the core promise: a harness problem never looks like an agent failure, and "not judged" is never green.

## Decision

1. **Composite action** `action.yml` at the repository root (`uses: Opsedia-ai/Opseval@<ref>`):
   - Inputs: `suite`, `profile`, `target`, `transport`, `repeats`, `filter` (one per line), `extra-args`, `python-version`, `opseval-spec` (empty = install from the action's own checkout), `comment`, `artifact-name`, `github-token`.
   - Outputs: `exit-code`, `gate`, `run-dir`.
   - Steps: install into a private virtualenv, run, upload `report.html`/`junit.xml`/`result.json`, write the job summary, then create or update **one** PR comment (identified by `<!-- opseval-gate -->`). The final step **exits with Opseval's exit code** (0/1/2/3).
   - Inputs reach scripts only through environment variables (no `${{ }}` inside `run:`), to prevent script injection. A failed comment (e.g. a read-only token on fork PRs) is a warning, never a failed gate.
2. **`opseval summary <run_dir>`** prints the Markdown gate summary. Scenario text is escaped (Markdown, mentions, HTML), and unmeasured values read "not measured". It is shared by the action and usable from any CI system.
3. **`pytest-opseval`** (a second workspace package, entry point `pytest11: opseval`):
   - Each scenario is one pytest item. A file runs once for all its items (only the selected ones after `-k`).
   - Mapping: passed → passed; failed → **FAILED**; inconclusive → **ERROR** (never a pass); skipped_precondition/blocked → skipped; a configuration or harness error for the file → ERROR on every item; an unloadable scenario → ERROR.
   - The plugin is inert without `--opseval-profile`/`opseval_profile`. Suite files are collected only when named explicitly, so directory collection never runs a scenario twice.

## Consequences

- The action installs from its own checkout until Opseval is on PyPI (Gate 0); afterwards `opseval-spec: opseval==X.Y.Z` pins a release.
- A CI job dogfoods the action against the demo agent (exit 0 healthy, exit 1 with a bug), so a broken action fails our own build.
- Opseval's own test run disables the plugin's entry point (`-p no:opseval`) so coverage measures imports correctly; the plugin's tests load it explicitly.
- A separate `opseval-action` repository (as the PRD names it) can wrap this action later without changing its contract.
