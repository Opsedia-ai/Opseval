# Opseval Governance

Opseval is a **company-led open-source project** stewarded by Opsedia, with a public roadmap and a path for community members to become maintainers.

## Roles

| Role | Who | Rights and duties |
|---|---|---|
| **Project lead** | Aya Salama (Opsedia) | Final say on roadmap, scope, releases and the open-core edition split. Bound by the pledges below. |
| **Core maintainers** | Opsedia engineers and invited community members | Merge rights on `main`; review, triage and release rota |
| **Component maintainers** | Community members who own a plugin area (a language pack, transport, trace source…) | CODEOWNERS for their paths; merge rights there |
| **Contributors** | Anyone with a merged contribution | Credited in release notes |
| **Community members** | Everyone in Discussions and Discord | Propose ideas, vote on the roadmap, help others |

**Contributor ladder:**
1. **Contributor:** one or more merged PRs.
2. **Triager:** invited after consistent, helpful triage.
3. **Component maintainer:** sustained ownership of an area; nominated by a maintainer and confirmed by lazy consensus.
4. **Core maintainer:** invited by the project lead.

## How decisions are made

| Change | Process |
|---|---|
| Bug fixes, docs, small features | Pull request, one maintainer approval |
| New plugin (scorer, language pack, transport, profile) | Pull request, component-maintainer approval, contract test kit passes |
| Public contracts (scenario schema, run record, score states, verdict states, exit codes, plugin APIs, REST API) | **RFC** in GitHub Discussions → 7-day comment period → decision recorded as an ADR in [`docs/adr/`](docs/adr/) |
| What is open source vs Enterprise | Project lead decides; rationale published in the changelog; bound by the pledges below |
| Code of Conduct enforcement | Conduct committee: the project lead plus one community member who is not employed by Opsedia |

## Our pledges

1. **Every evaluation capability is Apache-2.0,** with no usage limits in self-hosted Opseval. That covers scenarios, the simulator, transports, checkpoints, scorers, judges, verdicts, the release gate, re-scoring, calibration, reports and the web console.
2. **We never move a released open-source feature behind the licence key.**
3. **OSS, Enterprise and Cloud run the same codebase and data schema.** You can move between them without migration or lock-in.
4. **Telemetry never includes conversation content,** scenarios, prompts, hosts or credentials, and you can turn it off.
5. **The roadmap is public.** Community votes on ideas are an explicit input to prioritisation.

Enterprise modules live in [`ee/`](ee/) under a separate commercial licence. They cover organisation-scale needs: SSO enforcement, fine-grained RBAC, SCIM, audit logs, retention policies, protected suites and compliance evidence packs.

## Releases

- [Semantic Versioning](https://semver.org/). During `0.x`, minor releases may break compatibility and always come with migration notes. From `1.0`, breaking changes happen only in major releases.
- During `0.x`, a minor release ships every 4–6 weeks.
- Changelogs come from Conventional Commits, plus human-written highlights.

## Changes to this document

Changes to governance go through the RFC process and require the project lead's approval.
