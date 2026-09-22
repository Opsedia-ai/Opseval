# Opseval Enterprise Edition (`ee/`)

This directory is reserved for **Opseval Enterprise** modules. They will be source-available and need a licence key to run. See [`LICENSE`](LICENSE) (placeholder until counsel publishes the final text).

**Planned modules:** SSO enforcement (SAML / enforced OIDC) · fine-grained RBAC · SCIM provisioning · audit logs · per-project retention policies · protected suites and release approvals · compliance evidence packs.

**Rules**

- The Apache-2.0 core never imports from `ee/`. CI enforces this with an import-linter contract.
- Without a licence key, these modules stay dormant, and every open-source feature keeps working.
- Licence keys are verified offline. There is no phone-home, and air-gapped installs are supported.
- Community pull requests are not accepted into this directory. See [CONTRIBUTING.md](../CONTRIBUTING.md).

There is no code here yet.
