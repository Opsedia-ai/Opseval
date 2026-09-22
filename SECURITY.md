# Security Policy

Opseval handles credentials for the agents it tests and the LLM providers it calls, so we take security reports seriously.

## Reporting a vulnerability

**Please do not open a public issue.**

- Preferred: use GitHub's **private vulnerability reporting** ("Report a vulnerability" under the Security tab of this repository).
- Alternatively: email **security@opsedia.ai**. This address is being set up; until it's live, use private vulnerability reporting.

Please include the affected version or commit, steps to reproduce, the impact, and any suggested fix.

## What to expect

| Step | Target |
|---|---|
| Acknowledgement | within 3 working days |
| Initial assessment and severity | within 7 days |
| Fix or mitigation | within 30 days (critical: within 7 days) |
| Public disclosure | coordinated with you after a fix is available, or after 90 days |

We credit reporters in the advisory unless you prefer to stay anonymous.

## Supported versions

Opseval is pre-release. Once releases start:

- **0.x:** the latest minor release receives security fixes.
- **From 1.0:** the last two minor releases receive security fixes. Enterprise customers get long-term-support branches.

## Security design principles

These are the defaults Opseval is built around. Reports that show a way to break them are especially welcome.

- **Credentials go only to allowlisted hosts over TLS.** Plain `ws://` and `http://` are allowed only for allowlisted localhost addresses.
- **Secrets never reach results.** Tokens and keys are redacted from run records, reports, logs and the configuration digest.
- **Test tenants are isolated and locked.** A **committed-outcome guard** stops Opseval from answering the final approval before an irreversible action unless both the scenario and the run explicitly allow it.
- **Telemetry never contains conversation content,** scenarios, prompts, hosts or credentials, and can be turned off with `OPSEVAL_TELEMETRY=false`.
- **Air-gapped operation is supported.**
