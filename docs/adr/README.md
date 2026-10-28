# Architecture Decision Records

| ADR | Title | Status |
|---|---|---|
| [0001](0001-record-architecture-decisions.md) | Record architecture decisions | Accepted |
| [0002](0002-open-core-model.md) | Open-core model: Apache-2.0 core, `ee/` commercial modules, one codebase | Proposed |
| [0003](0003-python-core-openai-compatible-http.md) | Python core, Pydantic models, OpenAI-compatible HTTP only | Proposed |
| [0004](0004-react-console.md) | React + TypeScript web console | Proposed |
| [0005](0005-storage-abstraction.md) | One storage contract for local and server modes | Proposed |
| [0006](0006-three-state-scores-five-state-verdicts.md) | Three-state scores and five-state verdicts are a public contract | Proposed |
| [0007](0007-pluggable-transports.md) | Pluggable transports beyond WebSocket | Proposed |
| [0008](0008-committed-outcome-guard.md) | Committed-outcome guard is on by default | Proposed |
| [0009](0009-offline-licence-keys.md) | Offline-verifiable licence keys, no phone-home | Proposed |
| [0010](0010-telemetry.md) | Opt-out, content-free, documented telemetry | Accepted |
| [0011](0011-trace-backed-tool-scoring.md) | Trace-backed tool scoring with explicit clock-skew ambiguity | Proposed |
| [0012](0012-websocket-resume-and-token-refresh.md) | WebSocket resume and short-lived tokens | Proposed |
| [0013](0013-offline-rescore.md) | Offline re-scoring from a self-contained run folder | Proposed |
| [0014](0014-judge-votes-and-planted-injections.md) | Harness-axis persona judge, planted injections and 3-vote security judges | Proposed |
| [0015](0015-ci-integrations.md) | CI integrations: a composite GitHub Action and a pytest plugin | Proposed |
| [0016](0016-judge-independence-and-quality-judges.md) | Judge independence guard, plus policy, content-quality and efficiency judges | Proposed |
| [0017](0017-degraded-visibility-rubric-digests-and-ballot-independence.md) | Degraded evidence is visible in every output, rubric digests for every judge, independent ballots | Proposed |
| [0018](0018-judge-calibration-against-human-labels.md) | `opseval calibrate`: judge agreement with human labels, false passes reported apart | Proposed |
| [0019](0019-sse-transport.md) | Server-Sent Events transport, and which stream faults belong to the agent | Proposed |
| [0020](0020-preflight-health-checks.md) | Preflight health checks: a dependency outage skips, an unreachable agent refuses | Proposed |
| [0021](0021-telemetry-shipped-dark.md) | Telemetry implemented with no endpoint: nothing is sent until an operator sets one | Proposed |
| [0022](0022-scorer-plugin-api.md) | Scorer plugins over entry points, failing closed, with an honest contract kit | Proposed |
| [0023](0023-flow-map.md) | Flow map of expected vs observed phases; refuses to guess, changes no verdict | Proposed |
| [0024](0024-tool-argument-schemas.md) | `tools.args_valid` against a declared JSON Schema; vacuous passes are named | Proposed |
| [0025](0025-protocol-probes.md) | Protocol probes in the PROBES state; an error reply fails by default | Proposed |
| [0026](0026-published-json-schemas.md) | JSON Schemas generated from the models, with drift as a CI failure | Proposed |
| [0027](0027-sinks.md) | Sinks: webhook and Langfuse score push; cannot change a verdict, must not fail silently | Proposed |
| [0028](0028-otlp-trace-source.md) | OTLP spans read from the collector's file exporter; an empty trace is never fabricated | Proposed |
| [0029](0029-console-api.md) | The console API: the browser computes nothing, and no UI ships without an endpoint | Proposed |
| [0030](0030-closing-core-rule-leaks.md) | Four core-rule leaks closed: unjudged gates block, unmeasured scorers are named, connect timeouts and crashes are harness errors | Proposed |
| [0031](0031-securing-the-console-api.md) | Securing the console API: token, host and origin checks, the commit guard as a server flag, runs that always end visibly | Proposed |
| [0032](0032-gate-policy.md) | Gate policy per suite: presets and block/warn/ignore per outcome; facts stay fixed, policy is recorded | Proposed |
| [0033](0033-redacting-credential-values.md) | Credential values redacted from records; the marker keeps a leak gradeable | Proposed |
| [0034](0034-serve-the-console.md) | `opseval serve` serves the built console same-origin, with CSP and no framing | Proposed |
| [0035](0035-run-control.md) | Stop policies and cancellation: what is not run is reported as skipped, never dropped | Proposed |
| [0036](0036-judge-transcript-fence.md) | The judge's transcript is fenced data: JSON lines in a nonce-named fence, harness facts outside | Proposed |
| [0037](0037-retention.md) | `opseval prune`: delete runs older than N days, dry run unless `--yes`, never a run that may still be going | Proposed |
| [0038](0038-container-image.md) | A CLI container image built from the lock: digest-pinned, hash-checked, non-root, no console | Proposed |
| [0039](0039-docs-site.md) | The documentation site: MkDocs Material, generated reference pages, strict builds, not deployed before Gate 0 | Proposed |
| [0040](0040-github-issues-sink.md) | An issue for each failed critical scenario: one per scenario, no conversation content, bounded | Proposed |
| [0041](0041-test-tenant-pool.md) | Test-tenant pool and parallel lanes: exclusive file leases that err towards not running | Proposed |
| [0042](0042-read-the-reply-on-the-agents-clock.md) | Read the agent's reply, on the agent's clock: an always-on harness check that a profile read anything, and request/response turns timed from when sending began | Proposed |
| [0043](0043-defaults-from-the-profile.md) | Defaults from the profile, a silent agent fails, and every check declares (and has checked) the params it takes | Proposed |
| [0045](0045-jira-sink.md) | An issue for each failed critical scenario in Jira (Cloud and Data Center): the marker is a label | Proposed |
| [0046](0046-credential-provider-plugins.md) | Credential provider plugins: named to be used, fail closed, every failure a harness error | Proposed |
| [0047](0047-server-mode-on-postgres.md) | Server mode on Postgres, the Langfuse model: a versioned catalogue first, then runs, accounts and workers | Proposed |
| [0048](0048-guided-scenario-form.md) | The guided scenario form: the server offers the choices and writes the YAML, the loader judges it, the import saves it | Proposed |
| [0049](0049-tasks-in-a-project.md) | Tasks inside a project: one agent endpoint and one modality, with its own scenarios, suites, runs and labels; one folder is one task | Proposed |
| [0050](0050-accounts-and-access.md) | Accounts and access: argon2id users, OIDC, hashed sessions, org roles, project API keys; a network-facing server only behind TLS | Proposed |
| [0051](0051-runs-and-blobs-in-the-database.md) | Runs, scores, metrics and audio in Postgres and a blob store; `NULL` stays "not measured" | Proposed |
| [0052](0052-analytics-store.md) | An analytics interface with file and Postgres implementations; ClickHouse optional, in core, never required | Proposed |
| [0053](0053-voice-sessions-and-records.md) | Voice: audio sessions, audio in the record, a bridge to the text driver, and the one rule for voice faults | Proposed |
| [0054](0054-speech-providers.md) | Speech providers: OpenAI-compatible TTS and STT built in, others as plugins; allowlisted, failing closed, harness errors | Proposed |
| [0055](0055-full-duplex-voice-driver-and-metrics.md) | A full-duplex voice driver (barge-in, noise, code-switching) and voice metrics usable as gate thresholds | Proposed |
| [0056](0056-telephony.md) | Phone calls through a signed, TLS-only voice gateway; test numbers only | Proposed |
| [0057](0057-webrtc.md) | WebRTC rooms through an optional extra, shipped only if its dependencies pass the licence audit | Proposed |
| [0058](0058-ci-cd-anywhere.md) | CI/CD anywhere: a published GitHub Action and copy-paste workflows, server reporting, templates for other CI systems | Proposed |
| [0059](0059-languages-and-dialects.md) | Languages and dialects: BCP-47 packs, unknown languages refused at load, MENA then US/EU then Chinese | Proposed |
| [0060](0060-datasets-and-matrices.md) | Flexibility L1: datasets, logic-free placeholders and matrices, expanded at load with a cap | Proposed |
| [0061](0061-rubric-judges-and-content-checks.md) | Flexibility L2: teams' own rubric judges (fenced, hashed, calibrated) and content checks | Proposed |
| [0062](0062-generated-scenarios.md) | Flexibility L3: generated scenarios, drafts only, never labelled by a machine | Proposed |
| [0063](0063-adaptive-attacker.md) | Flexibility L4: an adaptive multi-turn attacker graded by the security judges | Proposed |
| [0064](0064-continuous-evaluation.md) | Flexibility L5: scheduled runs, regression alerts and proposed scenarios; only a human moves `production` | Proposed |
| [0065](0065-protecting-main.md) | Protecting `main`: a DCO check and a pre-push hook now, committed rulesets applied when the repository goes public | Proposed |
| [0066](0066-dashboards.md) | Dashboards: usage per call (input, output, reasoning, cached tokens), versioned model prices, cost, tokens, latency, quality and voice | Proposed |

New ADRs: copy [`template.md`](template.md), take the next number, and open a PR. Changes to public contracts start as an RFC in GitHub Discussions.
