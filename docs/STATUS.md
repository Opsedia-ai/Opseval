# Project status

**80% of the way to v1.0** — 33 of 41 roadmap items, as of 2026-10-08.

A project that refuses to print a number it cannot back should hold its own status report to the
same rule, so every figure here comes from a command you can rerun. How the percentage is
computed, and what it does not mean, is at the bottom.

| Milestone | Done | Share |
|---|---|---|
| v0.1 Alpha — *it runs end to end* | 13 / 13 | 100% |
| v0.2 Beta — *it gates releases* | 16 / 16 | 100% |
| v0.5 — *teams can share it* | 4 / 8 | 50% |
| v1.0 GA | 0 / 4 | 0% |
| **Total** | **33 / 41** | **80%** |

## Where the work actually is

**v0.1 and v0.2 are complete.** The evaluation core is done: scenarios and suites, five transports,
the simulator, deterministic scorers and LLM judges, verdicts and the release gate, reports, the
CI integrations, offline re-scoring and judge calibration.

**v0.5 is half done.** Shipped: the HTTP API, the web console, sinks (webhook and Langfuse score
push) and the OTLP trace source. Outstanding: accounts and API keys, Postgres and S3-compatible
storage with workers, live run streaming, and Docker Compose plus a Helm chart.

**v1.0 has not started.** Four items: frozen `opseval/v1` schemas and APIs, an external security
review, an Arabic UI, and the PyRIT recipe.

## The honest caveats

- **"Done" means the roadmap bullet is built, not that it is finished.** The console counts as
  done while run transcripts, live progress and a scenario editor are not built.
- **The remaining 20% is not 20% of the effort.** Accounts, Postgres, workers and an external
  security review are heavier than most of what is already green. Read this as *scope covered*,
  never as *time remaining*.
- **No release has shipped.** The version is `0.1.0.dev0` and PyPI holds a `0.0.1` placeholder.
  Tagging is blocked until Gate 0 (IP clearance) is signed off, which is a legal step, not a code
  one, and it gates everything regardless of this percentage.
- **The console's API has no accounts.** It is loopback-only and refuses to bind a real interface
  without a token (ADR-0029). That is a real limit on who can use it today.
- **Langfuse Cloud is unverified.** The trace reader is checked against a self-hosted 4.44 only.

## Health

| | | How to check |
|---|---|---|
| Tests | 850 passing | `make check` |
| Coverage | 94% (floor 85%) | `make check` |
| Type checking | clean, `mypy --strict`, 73 files | `uv run mypy` |
| Demo exit codes | 0 / 1 / 1 / 3 | `uv run opseval demo [--bug …]` |
| ADRs | 29 | `ls docs/adr/0*.md` |
| Scorers and judges | 21 | `uv run opseval list-scorers` |
| Transports | 4 (+ `python` callable) | `ls packages/opseval/src/opseval/transport/` |
| API routes | 16 | `grep -c '@app\.' …/server/app.py` |
| Console pages | 12 | `ls packages/console/src/pages/` |
| Merged PRs | 26 | `gh pr list --state merged` |

## Next, in order

1. **Gate 0: IP clearance.** Blocks any release. Not a code task.
2. **Run transcripts in the API and console** — the one thing people expect from run detail and
   cannot get, since records are already on disk with nothing serving them.
3. **Live run progress**, which needs the runner to report per-scenario state.
4. **Accounts and API keys**, which is what lets the server leave loopback.
5. **Postgres and S3-compatible storage with workers** (ADR-0005 already fixes the contract).
6. **Docker Compose and a Helm chart.**

## How the percentage is computed

Marker occurrences in [`ROADMAP.md`](../ROADMAP.md): 🟢 counts as done, ⚪ as outstanding. A line
can carry both — v0.5's console bullet is 🟢 for what shipped and ⚪ for what did not — so markers
are counted, not lines. "Beyond 1.0" (💡, not committed scope) and the Enterprise section (not in
this repo) are excluded.

```bash
python3 - <<'PY'
section, counts = None, {}
for line in open("ROADMAP.md"):
    if line.startswith("## "):
        section = line[3:].strip(); counts.setdefault(section, [0, 0]); continue
    if section and line.lstrip().startswith("- "):
        counts[section][0] += line.count("🟢"); counts[section][1] += line.count("⚪")
done = sum(c[0] for s, c in counts.items() if "💡" not in s and "Enterprise" not in s)
todo = sum(c[1] for s, c in counts.items() if "💡" not in s and "Enterprise" not in s)
print(f"{done}/{done + todo} = {done / (done + todo) * 100:.0f}%")
PY
```

Update this file whenever a roadmap marker changes — the roadmap is the source of truth, and this
page is a reading of it.
