# Opseval Roadmap

This is the public roadmap. Dates are targets, not promises. Vote on ideas in GitHub Discussions to influence priorities.

Legend: 🟢 in progress · ⚪ planned · 💡 exploring

## v0.1 Alpha: "It runs end to end" 🟢

- 🟢 Declarative YAML scenarios and suites, with fail-closed validation (`opseval validate`)
- 🟢 SUT profiles that map any agent's wire protocol to canonical frames
- 🟢 Transports: `http-json`, `websocket` (with session resume and short-lived HS256 token refresh), `openai-chat` (any OpenAI-compatible endpoint), `python` callable
- 🟢 Customer simulator: scripted and LLM personas, seeded runs, English + Arabic language packs (Gulf, Egyptian, MSA), keyboard-aware typing noise
- 🟢 Checkpoint policies (approve / reject / script), stop points, and the **committed-outcome guard**
- 🟢 Immutable run records with secret redaction
- 🟢 Deterministic scorers + LLM judges (quoted evidence, harness-computed scores): goal achievement, language consistency, persona consistency (harness axis)
- 🟢 Repeats, consistency, five-state verdicts, criticality-aware release gate, exit codes 0/1/2/3
- 🟢 Self-contained HTML + JSON report
- 🟢 `opseval demo` with a reference refund agent · 🟢 `opseval init` starter catalogues
- 🟢 JUnit XML output for CI test reports
- ⚪ Opt-out, content-free telemetry

## v0.2 Beta: "It gates releases" ⚪

- ⚪ Official GitHub Action (report artifact, PR comment) and `pytest-opseval`
- 🟢 Langfuse trace reader and trace-backed tool scorers (ordering, approval gating, call success) · ⚪ argument schemas
- 🟢 `opseval rescore` (offline, self-contained run folders) · ⚪ `opseval calibrate` (judge ↔ human agreement)
- 🟢 Prompt-injection planting and a 3-vote security judge (`security.no_injection_takeover`) · ⚪ `security.policy_adherence`, content-quality and efficiency judges
- ⚪ Flow map, preflight checks (🟢 pass^k and JUnit shipped early)
- ⚪ SSE transport and protocol probes
- ⚪ Stable scorer plugin API with a contract test kit

## v0.5: "Teams can share it" ⚪

- ⚪ `opseval serve`: REST API, live run streaming, Postgres + S3-compatible storage, workers
- ⚪ Web console: runs, live view, compare, trends, usage & cost, scenario editor, persona playground
- ⚪ Local accounts, API keys, OIDC login; organisations and projects
- ⚪ Docker Compose and Helm chart
- ⚪ Langfuse score push, OpenTelemetry trace source, webhooks

## v1.0 GA ⚪

- ⚪ Frozen `opseval/v1` schemas and APIs
- ⚪ External security review
- ⚪ Arabic UI
- ⚪ PyRIT integration recipe for adaptive multi-turn attacks

## Beyond 1.0 💡

- 💡 Scenario generation from policies or anonymised transcripts
- 💡 A2A transport
- 💡 More language packs: Levantine, Maghrebi, Turkish, Urdu, French, Hindi (contributions welcome)
- 💡 OWASP Top 10 for Agentic Applications / MITRE ATLAS tagging for security scenarios
- 💡 Voice and telephony (watching demand)

## Enterprise and Cloud

Organisation-scale features are developed in [`ee/`](ee/) and offered in Opseval Enterprise (self-hosted) and Opseval Cloud: SSO enforcement, fine-grained RBAC, SCIM, audit logs, retention policies, protected suites and compliance evidence packs. See [GOVERNANCE.md](GOVERNANCE.md) for what stays open source.
