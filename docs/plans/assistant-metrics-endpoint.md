# Assistant run metrics — endpoint built, dashboard deferred

**Scope note.** Records the decision behind `assistant_runs` aggregate reporting: what was
built, what is deliberately not built, and the conditions under which each deferred option
becomes cheap. Written 2026-09-01 before the build started; the endpoint option A chooses
shipped the next day as PR #151 (`d4d9efb`), so read A as the record of a decision rather than
as a proposal. B, C and D are still deferred and rejected in the state described here.

## Business problem

`assistant_runs` records one row per officer assistant request, served or refused, and has
done so since migration `db/migrations/0021_assistant_runs.sql`. Until PR #151, nothing read
it: there was no answer to "what fraction of assistant requests refused last week", "which
refusal code dominates", or "how slow is the loop under real use" — the data existed and no
path reached it. That is the problem this decision addresses. The reader that closes it is the
officer-gated `GET /assistant/metrics` recorded under "What shipped" below.

## Why LangSmith cannot answer this

The init DDL states the reason at `db/init/001_schema.sql` (the `assistant_runs` comment
block): the spans are content-free by design, and `trace()` is a no-op unless
`LANGSMITH_TRACING` is set. LangSmith answers "what did this one run do" and never "what
fraction of runs refused last week", because its population is whatever happened to be
exported. The table is written either way, which is what makes an aggregate over it honest.

The spans also omit `application_id` and `request_id` deliberately, so no per-application or
per-officer slice is available from the vendor even when tracing is on.

There is no second telemetry path to fall back on: no OpenTelemetry, Prometheus or statsd
exists anywhere under `services/` (`docs/specs/observability-week7.md`), and ADR 0015
rejected adopting an instrumentation framework as "a dependency and an operational surface"
for a smaller problem.

## Decision

**Build:** one read-only aggregate endpoint on `origination-service`, officer-gated.

**Defer:** the frontend panel, and any external BI tool.

## What shipped

Option A merged as PR #151 (`d4d9efb`): `GET /assistant/metrics` on `origination-service`,
officer-gated in the route body, over a `GROUP BY` aggregate in
`services/origination-service/app/assistant_runs.py`. Three departures from this plan are worth
carrying forward.

- **The aggregate is a SQLAlchemy read, not raw psycopg2.** The build instruction for this work
  directed raw psycopg2 "matching the file" and forbade an ORM read path. That had the seam
  backwards: read paths use SQLAlchemy and only money-moving writes stay on psycopg2, and a
  pooled connection is what keeps a full-window `GROUP BY` off the shared psycopg2 connection
  the money paths use.
- **Two bounds this plan did not ask for:** `STATEMENT_TIMEOUT_MS` (5000) and `MAX_GROUPS`
  (500), the latter returning a `truncated` flag so a partial sum cannot read as the whole
  window.
- **A read boundary this plan did not anticipate:** `outcome` and `policy_band` carry no CHECK
  constraint, so the aggregate serves only values in a vocabulary it knows and masks anything
  else.

That last rule is still unwritten as a decision. `scripts/spec_gate_map.txt` leaves
`assistant_runs.py` unmapped behind an explicit exception, and #151 widened that exception
rather than closing it, recording that this is the second time the surface moved ahead of it.
The privacy and export rules (`application_id` stored with no foreign key, aggregation as the
export boundary), the hand-applied-migration parity requirement, and this vocabulary mask are
owed an ADR. The exception itself is owned — `scripts/spec_gate_map.txt` line 320 names
maha-c, and `docs/kb.md` records the gap as a named exception with an owner — but no owner
distinct from the repo owner exists to escalate to, and nothing schedules the ADR.

## Options considered

### A. Read endpoint on origination-service — CHOSEN

A `GET /assistant/metrics` route beside the two existing assistant routes in
`services/origination-service/app/main.py`, calling `authz.require_officer(x_user_role)`, over
a `GROUP BY` aggregate written beside `record()` in
`services/origination-service/app/assistant_runs.py`.

No new container, no new database role, no new published port, no new credential, no new
dependency, no outbound network call. The gateway already proxies `GET /los/{path:path}`, so
the route is reachable as `/los/assistant/metrics` with no gateway change.

Rejected alternatives are rejected against this baseline.

### B. Frontend panel on `/admin` — DEFERRED, not rejected

The natural home. `frontend/app/admin/page.tsx` is where the `admin` role already lands
(`frontend/components/AppBar.tsx` maps the role to `/admin`, and `roleHome` in
`frontend/lib/api.ts` returns the same). It already fans out two `apiGet` calls.

Deferred for four reasons, in order of weight:

1. **Process.** The freeze is 2026-09-02. A new officer-facing feature with no spec, landing
   the day before, is the shape this project's own working agreement warns about.
2. **A third call in the existing `Promise.all` can blank the page.** That page has one
   `catch` and one `error` state; `Promise.all` rejects on first failure, so a metrics query
   that errors would also blank the applications and loans tables. The panel needs
   `Promise.allSettled` or its own loader — small, but it is a change to working code, not an
   addition beside it.
3. **No chart library.** `frontend/package.json` declares `next`, `react` and `react-dom` as
   its only runtime dependencies; the devDependencies are TypeScript and the
   vitest/testing-library stack, nothing that draws. Stat tiles and CSS bars are buildable
   today; a time series is a dependency conversation, and a time series is the first thing
   anyone asks for after seeing tiles.
4. **`make prove` cannot prove the frontend half.** It rolls back any tracked source file,
   `.tsx` included, but it only *executes* Python tests, so a commit whose only test is a
   `.tsx` file aborts as "changes no test file" (`scripts/prove_test.sh`). A panel test would
   still run blocking — the `frontend` job runs `npm run build` and `npm test` (vitest) with no
   `continue-on-error`, beside four existing `.test.tsx` suites — it just would not carry the
   red-without-fix / green-with-fix proof this project requires of a regression test.

None of 2–4 is disqualifying. The endpoint is the half that has to exist first either way,
and building it alone makes the panel a genuinely small follow-up.

### C. Grafana or Metabase over Postgres — REJECTED

Marketed as the no-code option. It is the expensive one here, for a reason specific to this
database.

`db/init/*.sql` and `db/migrations/*.sql` contain no `CREATE ROLE`, no `CREATE USER` and no
`GRANT`. Every service connects as the same role, against one shared Postgres and one schema.
That schema holds `applicants.ssn` — annotated `plaintext` in the init DDL — and
`payments.pan`, the full PAN that debt D13b leaves in place.

So pointing a BI tool at this database means either handing it a connection that reads
plaintext SSNs and full PANs, or building the first least-privilege role this project has
ever had: a migration, a scoped `GRANT` on `assistant_runs`, a revoke of everything else, a
credential in compose, and a test that the grant stays scoped. That is a security workstream,
undertaken to avoid exposing tables that the target table does not contain — `assistant_runs`
is PII-free by construction.

Add to that a container, a published port and a second login surface. Metabase is the worse
of the two: a JVM, its own metadata database, and a first-run browser wizard, which is poor
ground for a repeatable demo.

**Condition to revisit:** when the database has scoped roles for any other reason, this
becomes cheap and should be reconsidered — particularly if the ask grows to many dashboards,
ad-hoc SQL by non-developers, or alerting. None of those is the ask today.

### D. Published artifact or deck slide — REJECTED as the primary surface

A snapshot pasted into a hosted page. Costs about an hour and no PR, and is the right answer
if the only goal is showing a client a number on a Monday. It is not an operational surface
and does not read live data, so it does not replace A.

## What any consumer of this endpoint must carry

The aggregate is honest about its population only if the caller says what the population is.
Three separate reasons a request can exist with no row behind it:

- `assistant_runs.record` swallows every write failure by design and logs the exception class
  only. A telemetry outage reads as low volume, never as an error.
- The init DDL notes that a refusal code outside the `ck_assistant_runs_refusal_code`
  vocabulary is swallowed the same way, leaving a span, an answer for the officer, and no row.
- Pre-funnel refusals raised in the route bodies above the loop are not recorded. The 403 from
  `require_officer` on this very endpoint is one of them.

So the field is "refusal rate among recorded runs", and a UI label that shortens it to
"refusal rate" overstates what the number is. `config._run_database_probe` already carries a
readiness rung for the table, so an unapplied migration surfaces at `/health` rather than as a
silently empty dashboard — that rung is the thing to check first when the numbers look wrong.

## Consequences

- The question "what fraction refused last week" becomes answerable, by an officer, without a
  container or a database credential.
- No dashboard exists yet, so the answer arrives as JSON until B is built.
- Deferring B keeps the pre-freeze diff to one service, and leaves the panel as a follow-up
  small enough to be reviewed on its merits.
- Rejecting C leaves BI tooling unavailable, which is the status quo, and blocked behind
  least-privilege roles that this project should want for other reasons anyway.
