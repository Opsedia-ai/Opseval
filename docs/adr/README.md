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
| [0010](0010-telemetry.md) | Opt-out, content-free, documented telemetry | Proposed |
| [0011](0011-trace-backed-tool-scoring.md) | Trace-backed tool scoring with explicit clock-skew ambiguity | Proposed |
| [0012](0012-websocket-resume-and-token-refresh.md) | WebSocket resume and short-lived tokens | Proposed |
| [0013](0013-offline-rescore.md) | Offline re-scoring from a self-contained run folder | Proposed |
| [0014](0014-judge-votes-and-planted-injections.md) | Harness-axis persona judge, planted injections and 3-vote security judges | Proposed |
| [0015](0015-ci-integrations.md) | CI integrations: a composite GitHub Action and a pytest plugin | Proposed |
| [0016](0016-judge-independence-and-quality-judges.md) | Judge independence guard, plus policy, content-quality and efficiency judges | Proposed |

New ADRs: copy [`template.md`](template.md), take the next number, and open a PR. Changes to public contracts start as an RFC in GitHub Discussions.
