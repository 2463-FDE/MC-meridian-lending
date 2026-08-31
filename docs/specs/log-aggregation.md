# Spec: Log Aggregation, Retention and Trace Correlation

**Status:** Draft
**Companion ADR:** 0023 (not yet written — D6 below is the deliverable that writes it)
**Depends on:** ADR 0006 (logging redaction), `docs/specs/observability-week7.md` D1 (the
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
built log-only on purpose (`docs/specs/observability-week7.md` D1(e)), on the assumption that a
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
before it do not change retroactively. `docs/runbooks/operations.md` records that any `logs/*.log` written
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

`docs/specs/observability-week7.md` D1(e) is unambiguous: "No column, no migration… **This is the
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

**(b)** Retention, stated as what the mechanism actually delivers. `RotatingFileHandler` bounds
**disk, not age**: `LOG_MAX_BYTES x (LOG_BACKUP_COUNT + 1)` is a capacity ceiling, so a traffic
spike rolls history away in an afternoon and a quiet week keeps lines far past any stated period.
D1 therefore sets a **capacity target**, not a retention guarantee: size the ceiling from an
observed line rate so it holds roughly 30 days of that rate — the figure debt D5's own Mitigation
Path states and client Q1 has not confirmed — and record the observed rate and the arithmetic in
`docs/runbooks/operations.md` rather than leaving them implicit in two environment variables.

**(c)** The `except OSError: pass` becomes a logged warning on the stream handler that is
already installed. Silence stays the behaviour — a service must not fail to boot because a log
directory is missing — but the operator learns that file logging is off.

**(d)** No change to `RedactingFormatter`, to the redactor, or to the byte sequence either one
scans. This deliverable changes where lines go, never what they say.

**(e)** What D1 does **not** deliver, named here so no later reader mistakes the capacity target
for a retention control: an age floor ("at least 30 days is kept") or an age ceiling ("nothing
older than 30 days survives"). Both need a time-based purge, and this spec does not build one.
No acceptance criterion below claims an age guarantee, because none exists — R-1 and R-2 prove
the capacity ceiling holds and nothing more. The residual carries into D6(5), where retention and
access are decided together, and is re-asked as client Q1.

### D2. One formatter, and a drift rung that holds it

**(a)** Reconcile the five distinct copies to one, with the per-service log filename as the only
permitted difference. Adopt the five-service formatter (`%(name)s` included) as canonical, so
`payment-service` and `servicing-service` gain the logger name rather than the other five losing
it. Confirm against `docs/specs/observability-week7.md` D1(c), which quotes the two-service
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

**(d)** `docs/runbooks/operations.md` gains the procedure and the ordering rule: the audit runs, and is
clean, before any collector is pointed at the directory.

**(e)** This closes the "pre-redaction log files and any backups containing them are flagged
here but not audited" residual on debt D5. It closes no other row of that entry, and the entry's
status stays **Mitigated** — encoded PII (debt D14) and redaction at ingest remain open.

### D5. `request_id` on every hop

**(a)** Extend the week-7 convention beyond the payment span: `X-Request-Id` accepted on every
inbound route and forwarded on every outbound call in
`services/origination-service/app/clients.py` and every sibling HTTP client.

Minting happens at **entry points only**, and naming which services those are is the contract,
because minting per hop would give one request seven different ids and destroy the correlation
D5 exists to provide.

- `gateway` is the edge for every request from outside the deployment. It **mints** a `uuid4()`
  hex when the inbound header is absent, and that single value is what every downstream hop
  receives.
- `payment-service` and `decision-service` **already mint** today, each the entry point of its
  own path when reached directly — `new_request_id` at `services/payment-service/app/payments.py`
  under `docs/specs/observability-week7.md` D1(a), which names `decision-service`'s convention as
  the one it mirrors. D5 does **not** remove that; removing it would regress a merged week-7
  vector.
- `origination-service`, `servicing-service`, `kyc-service` and `disclosure-service` **never
  mint**. Each inherits the header or has none.

What D5 adds is a constraint, not a fourth behaviour: **a service that received an
`X-Request-Id` never re-mints**, so no request ever carries two ids.

**(b)** The vocabulary does not change. `docs/specs/observability-week7.md` D1(a) forbids a second
name for the same concept; `trace_id` and `correlation_id` stay out of the tree.

**(c)** Every log line on a request path carries `request_id=<value>` as a named field, and a
**non-minting** service that received no header logs `request_id=-`, so a call that bypassed the
gateway is visibly uncorrelated rather than silently untraceable — the week-7 rule, applied
everywhere. `-` is a value only a non-minting service can log: past D5(a) the gateway,
`payment-service` and `decision-service` always hold an id, minted when the caller supplied none,
so `request_id=-` on a line from any of those three is a defect, not an uncorrelated call.

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
   logging in the application. `docs/specs/observability-week7.md` D1(c) rules that re-encoding
   the log line changes the byte sequence the redactor scans under two blocking gates, and is
   therefore a change to a security control. That ruling stands; the ADR does not reopen it.
4. **The datastore constraint.** An aggregator is a datastore. The blocking
   `compose-hardening-gate` refuses a datastore on a host-reachable interface, and grades
   reachability rather than the presence of a `ports` key — a published port, any
   `network_mode`, and a network that is not provably a private bridge are each refused, across
   every filename Compose reads. Four separate escape routes have already been found and closed
   there (debt D21). The ADR budgets for that gate rather than discovering it at CI.
5. **Retention and access.** Who may read aggregated logs, for how long, and how that squares
   with D1, which sets a **capacity** target on the files and no age guarantee (D1(e)). If a
   retention *period* is required, the aggregator is where it becomes enforceable.
6. **Three options with rejection reasons**, per the repo's ADR standards.

### D7. Join the trace to the log

**(a)** Tag the LangSmith root run with `request_id` as run metadata. Metadata is not run input
or output, so `hide_inputs` / `hide_outputs` do not suppress it.

**This reverses a contract the tree already asserts, and that reversal — not the code — is the
deliverable's real cost.** `services/origination-service/tests/test_assistant_trace.py`
classifies `request_id` as a caller-linkable identifier and asserts it is absent: on the loop
root (`test_the_root_never_carries_caller_linkable_identifiers`) and again on **every** span
(`test_the_entry_span_never_carries_caller_linkable_identifiers`), both under the blocking
`agentic-loop-gate`. The rule is deliberate — an earlier PII-only reading was reversed during
PR #68's own review round — so "a minted opaque identifier is not officer or borrower content"
is the argument D7 must *win*, not a premise it may assume. Written as-is, D7(a) turns two
blocking assertions red.

**(b)** Log the LangSmith run id on the assistant's outcome line, beside `request_id`.

**(c)** The goal is a two-way walk: a trace names the log lines around it, and a log line names
the trace that produced it. **(b) alone delivers one direction** — log line to trace — and is the
fifteen-line change this deliverable was originally scoped as. The return direction is (a), and
it is gated by (e). D7 as a whole is therefore not the smallest deliverable in this spec; (b) is.

**(d)** No-op when tracing is off. `LANGSMITH_TRACING` defaults false, and D7 must not make the
untraced path do work or fail.

**(e)** D7(a) is **deferred pending ADR 0024** and is not schedulable until it lands. Shipping it
takes, in one PR: an ADR recording why a per-request minted identifier falls outside the
caller-linkable class those tests draw, an update to the trace privacy rationale the tests cite,
and a change to both assertions that names the ADR as its authority. Absent that ADR, the
correct state of the tree is the assertions as they stand. D7(b), (c) and (d) do not depend on
the decision and may ship first.

---

## Out of Scope (Not This Spec)

- **OpenTelemetry.** Spans, context propagation and a collector protocol across seven services
  is the honest general answer and a far larger change than D1–D7 combined. D5 delivers the
  identifier that any future OTel work would need anyway, so nothing here forecloses it. Named
  in `docs/debt-log.md`, not built.
- **JSON log encoding.** Ruled out in `docs/specs/observability-week7.md` D1(c) as a change to a
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
| A8 | A call with no `X-Request-Id` reaching a **non-minting** service directly (origination, servicing, kyc, disclosure) logs `request_id=-`, not an empty field and not a fresh id; the same call arriving **through the gateway** logs one gateway-minted id at every hop and no service logs `-` | D5(a), D5(c) |
| A9 | ADR 0023 exists and answers all six questions in D6 | D6 |
| A10 | Until ADR 0024 lands, `test_assistant_trace.py`'s absent-identifier assertions stay green and no span carries `request_id`. After it lands, a traced assistant run carries `request_id` in metadata with inputs and outputs still hidden | D7(a), D7(e) |
| A11 | With `LANGSMITH_TRACING=false`, D7 adds no call and no failure path | D7(d) |
| A12 | `docs/runbooks/operations.md` states the audit-before-collector ordering | D4(d) |

---

## Test Vectors

### Rotation and capacity
These prove the disk ceiling, which is all D1 claims. There is deliberately no age-based vector,
because D1(e) builds no age-based control.
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
- **R-12** A direct call to a non-minting internal service — servicing's `apply-payment`, the week-7 `V-TRACE-DIRECT` vector; assert `request_id=-`. The same direct call to `payment-service` or `decision-service` asserts a minted id, not `-`, because week-7 D1(a) makes both entry points.
- **R-12b** A call reaching `payment-service` **with** an `X-Request-Id`; assert the supplied value is used verbatim and no second id is minted — the no-re-mint constraint D5(a) adds.
- **R-13** A header value at and beyond the 64-character bound `decision-service` already enforces; assert the same bound, not a second convention.

### Trace join
- **R-14** Gated on D7(e). Until ADR 0024 lands the vector is the existing one, inverted: assert no span carries `request_id`. After it lands, assert `request_id` is present in run metadata and that the client still reports `_hide_inputs` and `_hide_outputs` true — the assertion `test_agentic_loop.py` already makes about boot hardening.
- **R-15** `LANGSMITH_TRACING=false`; assert the assistant path makes no LangSmith call and returns normally.

---

## Verification

Every regression test is watched failing before the code that satisfies it exists, then proven
with `make prove` from a detached worktree. Before handoff: the affected services' pytest, plus
`redactor-drift`, `redaction-tests`, `compose-hardening-gate` and `agentic-loop-gate` run
locally. D3 and D6 both touch compose surfaces the hardening gate grades; a gate failure at CI
on either is a wasted review round.

`scripts/spec_gate_map.txt` audits coverage: every tracked `docs/specs/*.md` must be mapped to a
code path or carry an `# EXEMPT:` line. This spec is mapped to the seven `logging_config.py`
copies, listed individually on the precedent set by the redactor entries — drift catches copies
that diverge, the map catches a copy that is deleted.

---

## Stated scope reductions

| Reading of the question | Delivered | Why |
|---|---|---|
| "Do we still need a collector and shipper?" | Yes, and D6 decides which — it does not build one | Choosing a vendor, a data-residency position and a datastore that satisfies debt D21's gate is an architecture decision, and this repo records those as ADRs before code |
| "Ship logs somewhere central" | Blocked behind D4 until the pre-redaction audit is clean | `docs/runbooks/operations.md` forbids shipping those files; the constraint predates this spec |
| "Structured logs" | Named `key=value` fields, parsed at the collector | The week-7 ruling on JSON encoding stands (D6(3)) |
| "Use LangSmith for this" | Not delivered, and P1 states why | One service, no payload, off by default, third-party |

---

## Client Questions

Raised in `docs/client-asks-2026-08-30-log-aggregation.md`. **None blocks D1–D5** — those five
are implementable today and each changes a constant at most. **Q2 and Q3 together decide whether
D6 is a week of work or an afternoon**, so they are asked before D6 starts rather than during it.

| # | Question | What the answer decides |
|---|---|---|
| Q1 | What log retention does the client's own policy require? | D1(b) and D1(e). The spec sizes a capacity target for roughly 30 days, taken from debt D5's own Mitigation Path; it delivers no age guarantee. A regulatory or contractual figure overrides the number and changes the arithmetic, not the design — but a figure the client must be able to *prove* at audit turns D1(e)'s residual into an age-based purge, which is work this spec does not contain |
| Q2 | May aggregated logs leave the client's network? | D6(1), outright. This is the question that collapses the option space: "no" removes every hosted service from consideration and makes D6 a choice among self-hosted collectors; "yes" reopens a vendor comparison and requires the data-residency position ADR 0023 has to state |
| Q3 | Is there an existing log platform in the client's estate to target? | Whether D6 adds a datastore to this stack at all. A target that already exists removes the aggregator container, and with it the whole debt D21 surface — the `compose-hardening-gate` refusals, the four escape routes already closed there, and the reachability argument ADR 0023 would otherwise have to make. A collector shipping to an address is a far smaller change than a collector plus a store |
| Q4 | Who may read aggregated logs, and for how long? | D6(5). Redaction lowers the stakes of the answer; it does not remove the question, because a redacted log still shows who did what to which loan and when |
| Q5 | Do pre-redaction log files exist on the client's own hosts, or only in development? | The scope of D4. The purge script is local; identical content elsewhere needs identical treatment, and the client is the only one who can say whether it is there |

**Dependency, stated plainly:** Q2 and Q3 are prerequisites for ADR 0023, not inputs to it. Writing
that ADR before both are answered means writing three options of which two are already excluded
by facts nobody asked for. D1–D5 proceed in the meantime and are not held behind any answer.

---

## Status

Draft. D1–D5 are implementable as written. D6 gates the collector and is the next ADR (0023).
D7 is split: (b)–(d) depend on nothing and may ship first; (a) is deferred pending ADR 0024,
which has to justify putting a caller-linkable identifier on a trace against the contract
`test_assistant_trace.py` holds today.
