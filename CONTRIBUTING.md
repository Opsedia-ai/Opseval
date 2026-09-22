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
make test          # ruff, mypy --strict, pytest
make demo          # runs the example refund agent + smoke suite
```

Requirements: Python ≥ 3.11 and [uv](https://docs.astral.sh/uv/). You need Node ≥ 22 only for the web console (from v0.5).

## Pull request checklist

- [ ] The PR is focused on one thing and links an issue (or explains why none is needed).
- [ ] Tests are added or updated. Coverage does not drop.
- [ ] `make test` passes locally (ruff, mypy strict, pytest, import-linter).
- [ ] **Harness faults map to `infra_error`.** Any new failure mode that comes from the tool itself, not the agent, must never produce `failed`.
- [ ] Docs are updated for user-facing changes.
- [ ] Commits follow [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`, `fix:`, `docs:` …).
- [ ] Every commit is signed off (see DCO below).

## Developer Certificate of Origin (DCO)

We use the [Developer Certificate of Origin](https://developercertificate.org/) instead of a CLA. Add a sign-off line to each commit:

```bash
git commit -s -m "feat(scoring): add tools.args_valid scorer"
# adds: Signed-off-by: Your Name <you@example.com>
```

By signing off, you certify that you wrote the contribution, or otherwise have the right to submit it under the Apache-2.0 licence.

**Clean contributions only.** Do not submit code, prompts, rubrics or test data copied from an employer's or client's private systems, or from any source whose licence isn't compatible with Apache-2.0.

## The `ee/` directory

Code under `ee/` is covered by the commercial Opseval Enterprise License and is written by the Opsedia team. We don't accept community pull requests into `ee/`. If you think an `ee/` capability belongs in the open-source core, open a Discussion. Our [governance pledge](GOVERNANCE.md) is that evaluation features are never moved behind the licence key.

## Code style

- Python: ruff (lint and format), mypy `--strict`, Pydantic v2 models with `extra="forbid"`.
- Every source file carries an SPDX header: `# SPDX-License-Identifier: Apache-2.0`.
- Error messages must tell the user what to do next.
- Never show an unmeasured value as `0`. Use `None` and render "not measured".

## Getting help

- Questions: GitHub Discussions (Q&A).
- Chat: the community Discord (link coming with the public launch).
- Security issues: see [SECURITY.md](SECURITY.md). Do not open a public issue.

Everyone in the community is expected to follow our [Code of Conduct](CODE_OF_CONDUCT.md).
