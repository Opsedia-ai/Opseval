# Epic specs

Each epic gets a short spec, merged here **before** implementation starts. A spec is 1–2 pages:

```markdown
# SPEC-NNN: <epic name>

## Problem
Who is blocked, and why does it matter?

## Behaviour
What the user sees and does, with a worked example based on the reference refund agent.

## Contracts touched
Scenario schema · run record · score states · exit codes · plugin API · REST API.
Changes to any of these need an RFC and an ADR.

## Acceptance criteria
- Given … when … then …

## Failure modes
Which harness faults can happen here, and how each maps to `infra_error`, an invalidated attempt, or exit code 3.

## Test approach
Unit, contract, wire-level, fault-injection, golden runs.
```

All examples use the fictional **online-store refund agent** (`examples/refund-agent`):
- phases `intake → lookup → proposal → confirmation`
- checkpoints `proposal_approval` and `supervisor_review`
- committed outcome `refund_issued`
- tools `lookup_order`, `check_eligibility`, `create_refund`, `notify_customer`
