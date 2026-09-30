# ADR-0013: Offline re-scoring from a self-contained run folder

- **Status:** Proposed
- **Date:** 2026-09-24
- **Deciders:** Opseval maintainers

## Context

Rubrics, judge prompts and scorer logic change. Teams must be able to tell *rubric drift* (we changed how we grade) from *agent drift* (the agent changed), without paying for, or risking, another conversation with the agent (PRD UJ-05, goal G4). ADR-0005 promised that readers need only the on-disk layout. But a run folder didn't contain the scenarios and profile it was scored against, so re-scoring needed the original catalogue files unchanged.

## Decision

1. **Every run is self-contained.** Next to the records, a run saves `profile.json` and `scenarios/<test_id>.json`: the profile and each scenario exactly as run, with suite merges and `--repeats` applied.
2. **`opseval rescore <run_dir>`** scores the saved records again with the current scorers and judges. It never opens a transport or starts the simulator; an import-linter contract enforces this. It uses the snapshot by default, or current files with `--catalogue` and `--profile`. Attempts are folded over the attempts that actually ran.
3. **The original is never modified.** Output goes to `<run_dir>/rescore-<UTC time>/{result.json, report.html, junit.xml}`. `result.json` carries `rescore: {of_run_id, rescored_at, scenario_source, scorer_changes, verdict_changes, reused_judge_scores}`, and the report shows a banner.
4. **Versions.** Every `ScoreResult` carries `scorer_version` (a class attribute, bumped when a scorer's logic changes). Judges already record `prompt_hash` and `model`. Re-scoring reports any scorer whose version, prompt hash or model differs from the original run.
5. **Judges without an LLM.** If a judge is configured, judges are re-run. If not, a judge score is reused from the original run only when its prompt hash is unchanged (marked `reused`). Otherwise it becomes `infra_error` (`JUDGE_UNAVAILABLE`), never a guess.
6. The exit code of `rescore` follows the new result (0/1/2/3), so a CI job can gate on a rubric change.

## Consequences

- Run folders grow by the size of the scenarios and profile (small, human-readable JSON).
- Runs made before this change have no snapshot. They can still be re-scored with `--catalogue` and `--profile`, and the result says `scenario_source: catalogue`.
- `ScoreResult.scorer_version` and `SuiteResult.rescore` are additive, optional fields: older results still load.
- Scorer authors must bump `version` when they change what a scorer measures.
