# ADR-0021: Telemetry, shipped dark

- **Status:** Proposed
- **Date:** 2026-10-02
- **Deciders:** Opseval maintainers
- **Implements:** [ADR-0010](0010-telemetry.md)

## Context

ADR-0010 specifies opt-out, content-free telemetry, and its detailed design ends with four
**decisions needed before implementation**. Two of those are product, privacy and legal calls that
code cannot make — which collector, operated by whom, under what data-processing terms, and
whether to enable it at public launch. Waiting for all four would have left the last v0.1 item
unbuilt indefinitely.

The detailed design itself offers the way through, as decision 3: *"Ship dark (code present,
endpoint unset, so nothing is sent) now."* This ADR records implementing exactly that, and which
of the open options were taken.

## Decision

The design in ADR-0010 is implemented in full, with **no default endpoint**. Nothing is sent by
anybody until an operator sets `OPSEVAL_TELEMETRY_ENDPOINT`, and `opseval telemetry show` on a
fresh install reports *"no endpoint configured, so nothing is sent"*.

Against ADR-0010's open decisions:

| ADR-0010 decision | Taken here |
|---|---|
| 1. Allowlist vs opt-out | **Option A**, its own recommendation: the telemetry host must be in `OPSEVAL_ALLOWED_HOSTS` and use HTTPS, exactly like every other host that receives data. This makes telemetry opt-in in practice. Revisit at public launch. |
| 2. Endpoint and operator | **Not taken.** No endpoint ships. This stays open, and needs a named operator and data-processing terms before anything is enabled. |
| 3. Timing | **Ship dark**, as offered. The code, the switches and the `show` command exist and are tested; the collector does not. |
| 4. `languages` and `demo_target` | **Kept.** They are in the payload ADR-0010 documents, they serve the activation and language-coverage KPIs, and a bare language code carries no content. Dropping them remains a cheap future change. |

**Enforcement is structural, not careful coding.** Three mechanisms, because "we were careful"
is not a privacy guarantee:

1. The payload is a Pydantic model with `extra="forbid"`, so a field cannot be added without
   moving the schema version and the documentation. A test pins the exact field set.
2. An import-linter contract forbids `opseval.telemetry` from importing records, the driver, the
   transports, the simulator, the traces or storage. It can reach a `SuiteResult` and nothing
   that holds a conversation.
3. A test builds a run whose scenario ids, scenario names, verdict reason, target host, profile
   name, run id, record paths, config digest and an `infra_error` reason are all the same sentinel
   string, and asserts that string appears nowhere in the serialised payload. If telemetry ever
   starts carrying content, that test fails.

**Counts are bucketed** (`0`, `1`, `2-5`, `6-20`, `21-100`, `100+`), so an exact number never
leaves the machine. **Only built-in names are sent**: a third-party transport, scorer id or trace
source becomes `custom`, because a bespoke name could identify a company.

**The install id is a plain UUID4**, created on first use and stored in the user config file. It
is deliberately *not* derived from a hostname, MAC address or path, since a derived id is a
fingerprint.

**It cannot affect a run.** One POST with a 2-second timeout, no retries, no disk queue, sent
after the result is written, with every exception swallowed — including an internal bug in the
telemetry code itself, which is caught and reported as a skipped send. A dead or slow collector
cannot change an exit code.

**Four independent ways to stop it**, any one sufficient: `OPSEVAL_TELEMETRY=false`,
`DO_NOT_TRACK=1`, `CI=true` (unless `OPSEVAL_TELEMETRY=true` is explicit), or
`opseval telemetry disable`. `CI` and `DO_NOT_TRACK` are read through `config.Settings` like
everything else, because the project rule is that no other module reads the environment.

The **first run that would send prints the notice and sends nothing**, so no event is ever sent
before the user has been told.

## Consequences

- The last v0.1 item is built and tested without pre-empting a privacy decision. What remains is
  a business choice — a collector and its terms — not engineering.
- Because Option A makes the host allowlisted, even an operator who sets an endpoint must also
  allowlist it. That is friction, and it is the point while the project is private: nobody turns
  telemetry on by accident.
- PR #12 should still be reviewed and merged on its own merits: it is the design record, and this
  ADR only records implementing it. If approval changes the design, the implementation follows,
  and the schema version moves.
- `languages` requires persona language codes, which a `SuiteResult` does not carry, so the
  runner passes them in. Telemetry still reads no records — but it does mean the payload builder
  takes inputs the result alone cannot supply, and a future field of that shape needs the same
  care about where it comes from.
- Nothing verifies the collector's behaviour, because there is no collector. ADR-0010's
  requirement that it must not store IP addresses is a contract on whoever operates it, and
  cannot be tested from here.
