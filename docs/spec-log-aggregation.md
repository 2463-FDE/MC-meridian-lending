# Spec: Log Aggregation, Retention and Trace Correlation

**Status:** Draft
**Companion ADR:** 0023 (not yet written — D6 below is the deliverable that writes it)
**Depends on:** ADR 0006 (logging redaction), `docs/spec-observability-week7.md` D1 (the
correlation id), ADR 0021 (the agent framework and root trace)

---

## Executive Summary

The platform has two observability surfaces and neither is a log pipeline.

**LangSmith** traces one service of seven, covers the LLM assistant surface inside it, carries
no payload by design, and is off unless an operator turns it on. It is the right tool for
inspecting an agent loop and the wrong tool for answering "what happened to this payment."

**The log files** are written by seven near-identical copies of `logging_config.py` to a host
directory that has no rotation, no retention, no size bound, and nothing reading it. The one
cross-service correlation mechanism the platform has — `request_id` on the payment span — was
built log-only on purpose (`docs/spec-observability-week7.md` D1(e)), on the assumption that a
place to search those lines would follow. It has not.

This spec builds that place, in the order the existing controls allow: retention and drift
first, the audit that unblocks shipping second, propagation third, the collector last and behind
its own ADR. Six of the seven deliverables add no dependency and no container.

---

## Problem Statement

### P1. LangSmith covers one service, one surface, and no payload

| | Measured on this tree |
|---|---|
| Services configured | One. `LANGSMITH_TRACING` / `LANGSMITH_API_KEY` / `LANGSMITH_PROJECT` appear only in the `origination-service` block of `docker-compose.yml`. The other six containers have no LangSmith environment at all. |
| Surface inside that service | The officer assistant. `assistant.entry` opens at the route funnel with the loop root inside it (ADR 0021 decision 5). Intake, boarding, the KYC hop and the decision hop are not traced. |
| Named residuals | The gateway hop and the route's pre-funnel 403/422 refusals, in ADR 0021's own Status. |
| Payload | Empty by construction. `harden_trace_client()` (`services/origination-service/app/llm/config.py`) claims the LangSmith singleton at boot with `get_cached_client(hide_inputs=True, hide_outputs=True)`, so inputs and outputs never leave the process. `LLM_TRACE_CONTENT` re-enables content, defaults false, is refused outside `ENVIRONMENT=development`, and no committed compose file may set it — the blocking `compose-hardening-gate` fails the build if one does. |
| Default | Off. `LANGSMITH_TRACING: ${LANGSMITH_TRACING:-false}`. |
| Data boundary | A third-party service outside the deployment. The hidden payload is why that is acceptable, and it is also why LangSmith cannot become the record of what the platform did. |

What a LangSmith trace therefore holds: span structure, timings, token metadata, and enum
refusal codes. What an incident on the money path needs: the request as it moved between
`payment-service` and `servicing-service`. There is no overlap. **Turning LangSmith on more
widely does not close any gap in this spec**, and this spec proposes no change to it beyond D7.

### P2. Log files grow without bound and are never retained

`logging_config.py` installs a `StreamHandler` and a plain `FileHandler` at
`${LOG_DIR:-logs}/<service>.log`. No rotating handler is configured in any of the seven copies,
and no retention exists anywhere in the tree. This is the open Week-2 row of debt **D5**, which
also names "centralized logging with redaction at ingest" as open.

The file handler is wrapped in `try: ... except OSError: pass`. An unwritable or unmounted log
directory therefore produces no file, no warning, and a service that appears to be logging
normally on stdout.

### P3. Pre-redaction history is on disk and unaudited

`PiiRedactor` merged in PR #2 (`1f89ac1`) and redacts every line written since. Lines written
before it do not change retroactively. `docs/runbook.md` records that any `logs/*.log` written
before that merge still holds plaintext PAN, CVV and SSN, that nothing has audited or deleted
them, and — explicitly — that they must not be shipped to a third-party aggregator.

**This is a build-order constraint, not a caveat.** A collector pointed at `./logs` before the
audit runs ships the exact content the redactor exists to prevent. D4 precedes D6.

### P4. The seven `logging_config.py` copies have already diverged

`redactor.py` is duplicated per service and held byte-identical by the blocking
`redactor-drift` job. `logging_config.py` is duplicated the same way with no equivalent rung,
and the copies no longer agree. Measured on this tree, ignoring the one line that legitimately
differs per service (the log filename), the seven copies reduce to **five distinct contents**.

Most of the difference is docstring wording. One is not:

| Services | Formatter |
|---|---|
| gateway, origination, decision, disclosure, kyc | `"%(levelname)s %(asctime)s %(name)s %(message)s"` |
| **payment-service, servicing-service** | `"%(levelname)s %(asctime)s %(message)s"` |

The two services missing `%(name)s` are exactly the two sides of the payment span the week-7
correlation id was built to join. A parser that keys on the logger name has nothing to key on
for the one span the platform can currently correlate.

### P5. The correlation id has nowhere to go

`docs/spec-observability-week7.md` D1(e) is unambiguous: "No column, no migration… **This is the
single most important scope boundary in this spec.**" That was the correct call — the persistent
correlation is `payments.id → payment_applications.payment_id`, and a second persistent key
would fight it.

The consequence is that finding a payment span today means grepping named fields across two
files in one host directory. That works on a single-container development stack. It does not
survive a second replica, a restarted container, or a question asked more than a rotation later.

### P6. The gateway's log file does not survive its container

Six services bind-mount `./logs:/app/logs`. The `gateway` block does not — it declares no
`volumes` key at all. `logs/gateway.log` is written into the container's writable layer and is
destroyed with the container.

The gateway is the only process that sees every request into the platform, and it is the sole
trust boundary. Its log is the one that is not kept.

### P7. Trace and log share no identifier

A LangSmith run carries a run id. A log line carries `request_id`. Nothing binds them. An
assistant incident visible in a trace cannot be walked into the log lines around it, and a log
line cannot be walked back into the trace that produced it.

### P8. Terminology, fixed here

This spec numbers its deliverables **D1–D7**. `docs/debt-log.md` also numbers entries `D<n>`.
They are different sequences and they collide. Every reference below to a debt entry is written
**debt D5**, **debt D14**, **debt D21**; a bare `D1` is a deliverable of this spec.

"Collector" means the process that reads log files and forwards them. "Aggregator" means the
store that receives them and answers queries. This spec keeps the two words distinct because
D6 may choose one vendor for both or two for each.

---

## Deliverables (In Scope)

### D1. Rotation, retention, and a visible failure when the log directory is unwritable

**(a)** Replace the plain `FileHandler` with `RotatingFileHandler` in all seven copies. Size and
backup count read from `LOG_MAX_BYTES` and `LOG_BACKUP_COUNT`, both with defaults, so an
operator changes them without a rebuild.

**(b)** Retention: 30 days, the figure debt D5's own Mitigation Path already states. Rotation
count and size are chosen to satisfy it for the observed line rate, and the arithmetic is
recorded in `docs/runbook.md` rather than left implicit in two environment variables.

**(c)** The `except OSError: pass` becomes a logged warning on the stream handler that is
already installed. Silence stays the behaviour — a service must not fail to boot because a log
directory is missing — but the operator learns that file logging is off.

**(d)** No change to `RedactingFormatter`, to the redactor, or to the byte sequence either one
scans. This deliverable changes where lines go, never what they say.

### D2. One formatter, and a drift rung that holds it

**(a)** Reconcile the five distinct copies to one, with the per-service log filename as the only
permitted difference. Adopt the five-service formatter (`%(name)s` included) as canonical, so
`payment-service` and `servicing-service` gain the logger name rather than the other five losing
it. Confirm against `docs/spec-observability-week7.md` D1(c), which quotes the two-service
formatter — the quote becomes stale and the spec is amended in the same PR.

**(b)** A `scripts/sync_logging_config.sh` mirroring `scripts/sync_redactor.sh`, and a CI check
that fails when the copies diverge outside the filename line. Extend the existing
`redactor-drift` job rather than adding an eighth blocking job — the duty is identical and the
job's own comment already describes it.

**(c)** The check must tolerate the one legitimate difference by construction, not by an
allowlist of seven filenames that rots when a service is added.

### D3. The gateway's log file survives its container

Add `./logs:/app/logs` to the `gateway` service in `docker-compose.yml`, matching the six
siblings. One line. It is separated from D1 only because it is a compose change and D1 is a
Python change; they may ship together.

This touches a file the blocking `compose-hardening-gate` grades. A bind mount is not a
published port and not a network declaration, so it does not engage that gate's refusals — but
the gate reads every filename Compose reads, and the change is verified against it locally
before handoff rather than assumed inert.

### D4. Audit and purge of pre-redaction log files

**(a)** A script that scans a log directory for lines matching the redactor's own patterns and
reports counts per file per pattern class. It reuses `redactor.py`'s expressions rather than
restating them, so a pattern the redactor gains is a pattern the audit gains.

**(b)** The report names files, line counts and pattern classes. **It never prints a matched
value**, and it never writes a match into its own output file. An audit that logs what it found
recreates the leak it is measuring.

**(c)** Purge is a separate, explicit invocation. Deleting a file that may be evidence is an
operator decision, taken once, recorded in the runbook.

**(d)** `docs/runbook.md` gains the procedure and the ordering rule: the audit runs, and is
clean, before any collector is pointed at the directory.

**(e)** This closes the "pre-redaction log files and any backups containing them are flagged
here but not audited" residual on debt D5. It closes no other row of that entry, and the entry's
status stays **Mitigated** — encoded PII (debt D14) and redaction at ingest remain open.

### D5. `request_id` on every hop

**(a)** Extend the week-7 convention beyond the payment span: `X-Request-Id` accepted on every
inbound route, minted as a `uuid4()` hex when absent, forwarded on every outbound call in
`services/origination-service/app/clients.py` and every sibling HTTP client.

**(b)** The vocabulary does not change. `docs/spec-observability-week7.md` D1(a) forbids a second
name for the same concept; `trace_id` and `correlation_id` stay out of the tree.

**(c)** Every log line on a request path carries `request_id=<value>` as a named field, and a
service that received no header logs `request_id=-`, so an uncorrelated call is visibly
uncorrelated rather than silently untraceable — the week-7 rule, applied everywhere.

**(d)** Log-only, still. No column, no migration. D5 propagates the field; it does not persist
it. The week-7 boundary holds.

**(e)** This is the deliverable that makes D6 worth paying for. A collector over prose lines with
no shared key is a more expensive `grep`.

### D6. ADR 0023 — the collector and the aggregator

A decision record, not an implementation. It must answer, at minimum:

1. **Self-hosted or third-party.** P1 establishes that payload leaving the deployment is the
   constraint LangSmith is already configured around. A log pipeline carries far more payload
   than a content-hidden trace. The ADR states the data-residency position explicitly.
2. **Redaction at ingest as a second layer.** Debt D5's Week-2 row asks for it by name. The
   in-process redactor stays the first layer; the collector's own rules are a second, and both
   fail closed.
3. **The parse format.** logfmt-style `key=value` extraction at the collector, **not** JSON
   logging in the application. `docs/spec-observability-week7.md` D1(c) rules that re-encoding
   the log line changes the byte sequence the redactor scans under two blocking gates, and is
   therefore a change to a security control. That ruling stands; the ADR does not reopen it.
4. **The datastore constraint.** An aggregator is a datastore. The blocking
   `compose-hardening-gate` refuses a datastore on a host-reachable interface, and grades
   reachability rather than the presence of a `ports` key — a published port, any
   `network_mode`, and a network that is not provably a private bridge are each refused, across
   every filename Compose reads. Four separate escape routes have already been found and closed
   there (debt D21). The ADR budgets for that gate rather than discovering it at CI.
5. **Retention and access.** Who may read aggregated logs, for how long, and how that squares
   with the 30 days D1 sets on the files.
6. **Three options with rejection reasons**, per the repo's ADR standards.

### D7. Join the trace to the log

**(a)** Tag the LangSmith root run with `request_id` as run metadata. Metadata is not run input
or output, so `hide_inputs` / `hide_outputs` do not suppress it and the content boundary is
unchanged — a `request_id` is a minted opaque identifier, not officer or borrower content.

**(b)** Log the LangSmith run id on the assistant's outcome line, beside `request_id`.

**(c)** The result is a two-way walk: a trace names the log lines around it, and a log line names
the trace that produced it. Roughly fifteen lines of code, and the smallest deliverable here.

**(d)** No-op when tracing is off. `LANGSMITH_TRACING` defaults false, and D7 must not make the
untraced path do work or fail.

---

## Out of Scope (Not This Spec)

- **OpenTelemetry.** Spans, context propagation and a collector protocol across seven services
  is the honest general answer and a far larger change than D1–D7 combined. D5 delivers the
  identifier that any future OTel work would need anyway, so nothing here forecloses it. Named
  in `docs/debt-log.md`, not built.
- **JSON log encoding.** Ruled out in `docs/spec-observability-week7.md` D1(c) as a change to a
  security control. D6(3) restates the ruling; neither reopens it.
- **Metrics, dashboards and log-based alerting rules.** A store is a prerequisite for all three.
  Revisit once D6 has an implementation.
- **The reconciliation alert's recipient.** Week-7 D4 shipped an alert; who receives it is an
  open client question, tracked with the client asks. A collector gives that alert somewhere to
  originate from, which is an argument for D6 and not a deliverable of it.
- **Any change to `redactor.py` or its patterns.** Encoded PII stays deferred under debt D14.
- **Rotating the leaked bureau, core-banking and processor keys.** Debt D1, open, unrelated.
- **Persisting logs or `request_id` to Postgres.** See D5(d) and the week-7 boundary it inherits.
- **Retrofitting `request_id` onto rows already written.** D5 applies forward.
- **Widening LangSmith to more services.** P1 explains why it would not help.

---

## Acceptance Criteria

| # | Criterion | Holds |
|---|---|---|
| A1 | No `logging_config.py` copy installs a non-rotating file handler | D1(a) |
| A2 | A log directory that cannot be created produces a warning on stdout, and the service still boots | D1(c) |
| A3 | The seven copies differ only in the log filename, and CI fails when they do not | D2(a), D2(b) |
| A4 | `payment-service` and `servicing-service` log lines carry the logger name | D2(a) |
| A5 | `logs/gateway.log` exists on the host after `make up` and a request through the gateway | D3 |
| A6 | The audit script reports a pattern-class count and prints no matched value | D4(a), D4(b) |
| A7 | An inbound `X-Request-Id` reaches every downstream service unchanged | D5(a), D5(b) |
| A8 | A call arriving with no header logs `request_id=-`, not an empty field or a fresh id | D5(c) |
| A9 | ADR 0023 exists and answers all six questions in D6 | D6 |
| A10 | A traced assistant run carries `request_id` in metadata, with inputs and outputs still hidden | D7(a) |
| A11 | With `LANGSMITH_TRACING=false`, D7 adds no call and no failure path | D7(d) |
| A12 | `docs/runbook.md` states the audit-before-collector ordering | D4(d) |

---

## Test Vectors

### Rotation and retention
- **R-1** Write `LOG_MAX_BYTES + 1` bytes to one logger; assert exactly one backup file exists.
- **R-2** Write past `LOG_BACKUP_COUNT` rotations; assert the oldest file is gone and the count is bounded.
- **R-3** Point `LOG_DIR` at an unwritable path; assert the service boots, the stream handler works, and a warning names the directory.

### Drift
- **R-4** Mutate one copy's formatter; assert the drift check fails and names the file.
- **R-5** Mutate only a copy's log filename; assert the drift check passes.
- **R-6** Add an eighth `services/*/app/logging_config.py`; assert the check grades it without an edit to the check.

### Audit
- **R-7** A file containing a PAN, an SSN and an email; assert the report counts three classes and that no matched substring appears anywhere in the output.
- **R-8** A file containing only redacted lines; assert a clean report and exit code 0.
- **R-9** A file the script cannot read; assert a non-zero exit distinct from "found matches" — could-not-run and clean must not share a code, the rule `reconciliation-gate` already enforces.

### Correlation
- **R-10** `POST` with `X-Request-Id: abc`; assert every service's log line for that request carries `request_id=abc`.
- **R-11** Same call with no header; assert one id is minted at the edge and reused downstream, not re-minted per hop.
- **R-12** A direct call to an internal service; assert `request_id=-`.
- **R-13** A header value at and beyond the 64-character bound `decision-service` already enforces; assert the same bound, not a second convention.

### Trace join
- **R-14** Traced assistant run; assert `request_id` is present in run metadata and that the client still reports `_hide_inputs` and `_hide_outputs` true — the assertion `test_agentic_loop.py` already makes about boot hardening.
- **R-15** `LANGSMITH_TRACING=false`; assert the assistant path makes no LangSmith call and returns normally.

---

## Verification

Every regression test is watched failing before the code that satisfies it exists, then proven
with `make prove` from a detached worktree. Before handoff: the affected services' pytest, plus
`redactor-drift`, `redaction-tests`, `compose-hardening-gate` and `agentic-loop-gate` run
locally. D3 and D6 both touch compose surfaces the hardening gate grades; a gate failure at CI
on either is a wasted review round.

`scripts/spec_gate_map.txt` audits coverage: every tracked `docs/spec-*.md` must be mapped to a
code path or carry an `# EXEMPT:` line. This spec is mapped to the seven `logging_config.py`
copies, listed individually on the precedent set by the redactor entries — drift catches copies
that diverge, the map catches a copy that is deleted.

---

## Stated scope reductions

| Reading of the question | Delivered | Why |
|---|---|---|
| "Do we still need a collector and shipper?" | Yes, and D6 decides which — it does not build one | Choosing a vendor, a data-residency position and a datastore that satisfies debt D21's gate is an architecture decision, and this repo records those as ADRs before code |
| "Ship logs somewhere central" | Blocked behind D4 until the pre-redaction audit is clean | `docs/runbook.md` forbids shipping those files; the constraint predates this spec |
| "Structured logs" | Named `key=value` fields, parsed at the collector | The week-7 ruling on JSON encoding stands (D6(3)) |
| "Use LangSmith for this" | Not delivered, and P1 states why | One service, no payload, off by default, third-party |

---

## Client Questions

None blocks D1–D5. Each changes a constant or scopes D6.

- **Q1** What log retention does the client's own policy require? D1(b) assumes 30 days from
  debt D5's Mitigation Path; a regulatory answer overrides it.
- **Q2** May aggregated logs leave the client's network? Decides D6(1) outright.
- **Q3** Is there an existing log platform in the client's estate to target, rather than a new
  one in this stack? A target that already exists removes D6's datastore-in-compose problem.
- **Q4** Who may read aggregated logs? D6(5); redaction reduces the answer's stakes but does not
  remove the question.
- **Q5** Are the pre-redaction log files on the client's own hosts as well as in development?
  D4's purge is a local script; the same content elsewhere needs the same treatment.

---

## Status

Draft. D1–D5 are implementable as written. D6 gates the collector and is the next ADR (0023).
D7 depends on nothing and may ship first.
