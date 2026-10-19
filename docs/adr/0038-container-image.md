# ADR-0038: A CLI container image, built from the lock

- **Status:** Proposed
- **Date:** 2026-10-09
- **Deciders:** Opseval maintainers

## Context

FR-DX-01 (a must-have) asks for `pip install` and a container image. Many CI systems run steps
in containers, and a team without a Python toolchain should still be able to gate a release.
No release exists before Gate 0, so the image is built from a checkout, not from PyPI.

An evaluation harness holds an agent's credentials and LLM keys while it runs, so the image is
a supply-chain surface.

## Decision

A `Dockerfile` at the repository root builds a **CLI image** (`ENTRYPOINT ["opseval"]`):

- **Pinned and reproducible.** Base images are pinned by digest (Dependabot keeps them current).
  Runtime dependencies are installed from `uv export --frozen` with `pip --require-hashes`, so
  the image has exactly the locked versions and a tampered package fails the build.
- **The build backend is locked too.** Hatchling sits in a `build` dependency group in
  `uv.lock`. It is installed hash-checked in the build stage, and the wheel is built without
  isolation and **without a network** (`RUN --network=none`), so nothing unchecked enters the
  image. The digests are written out on each `FROM` line, because Dependabot reads those, not
  `ARG` defaults.
- **Least privilege.** It runs as an unprivileged user (uid 10001) in `/work`, with no compilers
  and no `serve` extra.
- **An allowlist build context.** `.dockerignore` sends only what the build reads. Everything
  else stays out, including `docs-internal/` (confidential), `.env` files and run reports.
- **No console.** `opseval serve` binds to loopback only (ADR-0031), which a container cannot
  be reached on. Relaxing that for Docker would expose a token-protected API that can start runs
  with the operator's credentials, so the console stays a run-on-your-machine tool.

CI builds the image on every change and checks the four demo exit codes (0/1/1/3) inside it,
and that it does not run as root.

## Consequences

- `docker run --rm -v "$PWD:/work" opseval run …` works in any CI. On Linux, pass
  `--user "$(id -u):$(id -g)"` so reports written to a mounted folder belong to you.
- Publishing the image (GHCR) is a release step and waits for Gate 0, like PyPI.
- The image is about 290 MB (Python slim plus the locked dependencies). A distroless base could
  shrink it later, at the cost of a shell for debugging.
- *Update (2026-10-19):* the image's Python stays on a version CI tests (3.11–3.13). Dependabot
  keeps the 3.12 digest current but no longer proposes a new Python version; that move comes
  with a change to the CI matrix.
