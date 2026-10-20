# ADR-0040: An issue for each failed critical scenario (GitHub Issues)

- **Status:** Proposed
- **Date:** 2026-10-19
- **Deciders:** Opseval maintainers

## Context

FR-INT-05 asks for an issue publisher (GitHub Issues, Jira) for failed critical scenarios.
Teams want a release-blocking failure to land where they track work, not only in a CI log.
It is a sink (ADR-0027), so it runs after the verdict and can never change it, and a failure
to publish is reported, never swallowed.

An issue tracker is a different audience from the run report, often a wider one: more people,
sometimes a public repository. A transcript can hold personal data.

## Decision

A built-in `github-issues` sink, configured by `OPSEVAL_GITHUB_ISSUES_REPO` (`owner/name`),
`OPSEVAL_GITHUB_TOKEN` (a secret, redacted everywhere), `OPSEVAL_GITHUB_API_URL` (for GitHub
Enterprise; its host must be allowlisted like every destination) and `OPSEVAL_GITHUB_ISSUES_MAX`.

- **Only real agent failures that matter.** It reports scenarios whose verdict is `failed` and
  whose criticality is `critical` or `blocker`. Inconclusive, skipped and blocked scenarios are
  not the agent failing, so they never become issues.
- **One issue per scenario.** A hidden marker (`<!-- opseval:<suite>:<id> -->`, percent-encoded,
  so two names never share one) finds the scenario's open issue, labelled `opseval`. A repeat
  failure comments on it rather than opening another, including a second failure in the same
  run. Every open labelled issue is read (up to 2,000; beyond that the sink stops rather than
  guess). A closed issue is not reused. Issues are never closed automatically, because one pass
  does not prove a flaky failure fixed; people close them.
- **Fail closed on the label.** Finding an issue depends on the label. If GitHub creates an
  issue without it (a token that cannot apply labels), the sink stops and fails, because every
  later run would open a duplicate.
- **No conversation content, no URLs.** An issue carries the scenario id and name, criticality,
  the failed checks, the pass rate, the run id, the suite, the **profile name** and the gate.
  Judge quotes and turns stay in the report, and so does the target URL, which can carry a
  credential or an internal host. In the body, scenario-supplied text is a code span, where
  GitHub renders no mention, link or issue reference. In the title, `@` and `#` are broken so
  they notify no one.
- **Bounded.** At most `OPSEVAL_GITHUB_ISSUES_MAX` (10) issues are opened or commented on per
  run; the outcome counts the rest.
- **Partial failure is reported in full.** One issue that cannot be written does not stop the
  others. The sink then fails, and its outcome names what it did write and what it could not.

## Consequences

- The token needs permission to write issues in that one repository: a fine-grained token with
  Issues read and write.
- Jira followed with the same rules, its marker a label (ADR-0045).
