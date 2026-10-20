# ADR-0045: An issue for each failed critical scenario (Jira)

- **Status:** Proposed
- **Date:** 2026-10-20
- **Deciders:** Opseval maintainers

## Context

FR-INT-05 asks for an issue publisher for GitHub Issues and Jira. ADR-0040 built the GitHub
one and set its rules: only real failures of critical scenarios, one issue per scenario, no
conversation content and no URLs, bounded, and a partial failure is reported. Jira needs the
same rules, but it has no hidden comment for a marker, and Cloud and Data Center differ in
authentication and in their search API.

## Decision

A built-in `jira` sink, configured by:

- `OPSEVAL_JIRA_URL`, whose host must be allowlisted and use TLS;
- `OPSEVAL_JIRA_PROJECT`, a project key matching `[A-Z][A-Z0-9_]*`. It is checked because it
  goes into JQL.
- `OPSEVAL_JIRA_TOKEN`, a secret redacted everywhere;
- `OPSEVAL_JIRA_EMAIL`, set for Cloud and left unset for Data Center;
- `OPSEVAL_JIRA_ISSUE_TYPE`, default `Bug`;
- `OPSEVAL_JIRA_ISSUES_MAX`, default 10.

Everything in ADR-0040 applies. In Jira's terms:

- **The marker is a label.** Every issue carries `opseval` and `opseval-<20 hex digits of
  sha256(suite NUL scenario id)>`. Jira labels cannot contain spaces, and a digest stays short
  and one-to-one. One JQL search per run (`project = KEY AND labels = opseval AND
  statusCategory != Done`) reads every open issue, up to 2,000. Beyond that the sink stops
  rather than guess. Done issues are not reused, and none is ever moved to Done automatically.
- **Fail closed on the label.** The labels go in the create request itself. A project that
  cannot set them answers 400, so the create fails visibly and no unlabelled duplicate is
  opened.
- **Inert text.** Scenario-supplied text sits inside `{noformat}`, where Jira renders no
  mention, link or macro. A `{noformat}` inside the text is broken so it cannot close the
  span early. In the summary, `[~` (a user mention) is broken.
- **Cloud and Data Center.**
  - With an email set, the sink is talking to Cloud: basic auth with an API token, and search
    through `POST /rest/api/3/search/jql` (token-paginated; the old `/search` is retired there).
  - Without one, it is talking to Data Center: the token is a personal access token sent as a
    bearer, and search goes through `GET /rest/api/2/search`.
  - Both create issues and comments through the v2 API, which takes a plain wiki-markup string
    rather than Atlassian Document Format.

## Consequences

- The account needs permission to browse, create and comment on issues in that one project.
- FR-INT-05 is done for both trackers. Both sinks are tested against fakes of their APIs.
  Neither has been run against a live tracker, which needs a test project and is tracked in
  `docs/ATTENTION.md`.
