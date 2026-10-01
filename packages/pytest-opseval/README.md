# pytest-opseval

Run [Opseval](../../README.md) scenarios as pytest tests: one test per scenario.

```bash
pip install pytest-opseval
pytest --opseval-profile opseval/profile.yaml --opseval-target wss://agent.example.com \
  opseval/suites/smoke.yaml
```

Options (or the matching ini keys `opseval_profile`, `opseval_target`, `opseval_transport`, `opseval_reports_dir`): `--opseval-profile`, `--opseval-target`, `--opseval-transport`, `--opseval-repeats`, `--opseval-allow-commit`, `--opseval-reports-dir`.

The mapping keeps Opseval's promise that a harness problem never looks like an agent failure:

| Opseval verdict | pytest outcome |
|---|---|
| passed | passed |
| failed | **FAILED** |
| inconclusive (not judged) | **ERROR**, never a pass |
| skipped_precondition, blocked | skipped (with the reason) |

The plugin does nothing unless `--opseval-profile` is set. Scenario files are collected from any path you give pytest; suite files only when you name them explicitly.
