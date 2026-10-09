# ADR-0039: The documentation site

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** Opseval maintainers

## Context

FR-DX-04 (a must-have) asks for a documentation site with a quickstart, concepts, a SUT
profile guide, a scorer reference and a cookbook. Until now there was a long README and the
ADRs. The architecture document names MkDocs Material.

A reference page written by hand goes stale without anyone noticing: a scorer added, a setting
renamed, a default changed.

## Decision

- **MkDocs Material**, from `docs/` (`mkdocs.yml` at the root), in the dev dependencies.
  `mkdocs<2` is pinned, because MkDocs 2 drops the plugin and theme system Material is built on.
- **Hand-written guides:** home, quickstart, concepts, SUT profile guide and cookbook
  (`docs/guide/`).
- **Generated reference:** `scripts/reference_docs.py` writes `docs/reference/scorers.md` from
  the scorer registry and `docs/reference/settings.md` from `config.Settings`. To make the
  settings page complete, every setting now has a docstring and the model reads them
  (`use_attribute_docstrings`). `make check` and CI fail when either page is stale, as they do
  for the JSON Schemas.
- **The ADRs are part of the site**, reachable from their index. Internal process documents
  (STATUS, ATTENTION, specs) are excluded.
- **Strict builds:** `mkdocs build` runs in `make check` and CI with `strict: true` and link and
  anchor validation, so a broken link fails the build. `--quiet` must not be used: it hides the
  warnings strict mode counts, and a broken link would pass.
- **No deployment yet.** A published site is public, and the repository stays private until
  Gate 0. Deploying (GitHub Pages) is a release step.

## Consequences

- Adding or changing a scorer or a setting means running `uv run python scripts/reference_docs.py`.
- The README keeps its role as the front page. The site goes deeper, and the two will overlap
  until the README is trimmed after launch.
- An Arabic edition (planned) can be added with Material's i18n support later.
