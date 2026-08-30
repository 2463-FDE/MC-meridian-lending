# Cards — decisioning governance (deferred work)

**Raised:** 2026-08-13 · **Source:** Lending Ops' answers to the week-8 governance questions,
transcribed on the `docs/client-asks` branch (working log) and `docs/client-asks-originals`
(verbatim record) — neither is on this branch, so the path is named rather than linked ·
**Cycle:** code freeze Friday 2026-08-28, cycle ends Friday 2026-09-04

Two pieces of work deliberately out of this cycle. G1 the client deferred by name and asked to see
carded. G2 nobody asked for — it comes from our own plan, and this card is the record of choosing
not to build it now.

Card ids are prefixed `G` so they do not collide with `docs/cards-week6-servicing.md`'s C1–C5 when
a spec cites both.

Estimates are engineering days for one person. They are planning figures, not commitments.

---

## G1 — The adverse-action notice channel

**What.** A notice channel this platform owns: a persisted notice record per adverse decision
carrying the reasons actually sent, an emission path to whoever prints and mails, delivery
tracking, the 30-day Reg B clock computed rather than kept by hand, and evidence a letter went
out that survives an examination.

**What exists today**, in the client's own words: the letters are produced in the origination back
office. Someone exports the decision, a print-and-mail vendor sends the letter, and the 30-day
clock lives on a spreadsheet. This platform holds none of it — no notice record, no send, no
delivery status, no timing column. `decision_events.decided_at` is a stamp, not a deadline.

**Why deferred.** The client's explicit decision: *"Building an actual notice channel — delivery
tracking, the clock instrumented, evidence a letter went out — is real work and it's not what this
cycle is for."* She also drew the boundary the design must respect: the notice channel is **not**
this platform today and she does not want it to become this platform by accident. Anything built
here is built on purpose, with her agreement.

**The consequence to state plainly:** until this lands, **delivery cannot be proven.** The
monitoring spec names this gap and references this card, at the client's instruction, so nobody
reads the spec and assumes otherwise.

**Estimate.** 8–12 days, and the range is wide because one input is not ours. Roughly: 3–4 days
for the notice record and its emission path (the record must snapshot the reasons *as sent*, not
re-derive them later, or the evidence is a recomputation); 2–3 days for the clock — computed from
the decision, surfaced as an overdue queue, not a column someone updates; 2–3 days for the officer
view and the retention path (~25 months, Reg B, already in `policies/underwriting_guidelines.md`).
**Plus vendor integration, unestimated** until the vendor and its file or API contract are named —
a flat-file drop and a REST integration with delivery callbacks are not the same week.

**Depends on.**
- The reason-truncation fix, so what the channel sends is all four reasons and not one — PR #34.
  Landing the channel on top of the truncated field would industrialise the defect.
- `decision_events.principal_reasons` as the source of record — exists,
  `db/init/001_schema.sql:152`.
- **Client-side, and this is the blocker:** the vendor's name and its intake contract, and who owns
  the spreadsheet clock today. Until Lending Ops names both, the estimate above cannot close.

**Owner.** Engineering builds it. Lending Ops names the vendor, the contract, and the current
clock owner — the card cannot start without those three.

**When.** Next cycle, starting Monday 2026-09-07. Not before: this cycle's freeze is Friday
2026-08-28 and the client named the governance package and the balance-correctness work as the two
things this must not displace.

---

## G2 — RAG-drafted adverse-action wording

**What.** Retrieval over the policy documents drafts the *sentence* an applicant reads, while the
reason codes stay deterministic from the model's real drivers. A compliance reviewer approves the
wording before it goes out.

**Why deferred, and this one is different from G1:** the client did not ask for it. It comes from
our own weeks 7–10 plan, whose W8 row lists it as a design principle and whose Path B calls it a
reach — *"a compliance review queue for drafted adverse-action wording, ahead of the client's own
timeline for it."* Lending Ops' answers scoped this cycle without it. Building it now would spend
freeze-week capacity on something no one asked for, against a client who named exactly what she
did not want displaced.

**The design principle survives the deferral and is worth recording here**, because it is the part
that must not be got wrong later: 12 CFR 1002.9 requires the statement to describe the reasons
*actually used*. A generated reason is a guess about a decision already made, so **generation must
never sit on the causal path** — codes come from real attributions, the model only phrases them.
`services/decision-service/app/reasons.py` already holds the deterministic half.

**Depends on.**
- **G1.** Drafted wording with no channel to carry it goes nowhere. Ordering this before G1 builds
  a sentence nothing sends.
- Policy retrieval that actually answers. **Closed 2026-08-23 (PR #64).** `search_policy` is
  the third entry in `assistant.py`'s `_TOOLS`, backed by
  `services/origination-service/app/policy_retrieval.py` over the committed `policies/`
  corpus. The original wording — that `rag_eval/` was an offline harness with no retrieval
  tool wired into the assistant — was true when this card was written and is kept in the
  history, not here, because a dependency list that still names a closed dependency reads
  as blocked work.
- A compliance review queue, so a human approves wording before send — Path B in the plan, also
  unbuilt.

**Estimate.** Not estimated, deliberately. Two of its three dependencies are themselves unbuilt and
one of them (G1) is unestimated pending a client answer; a number here would be invented.

**Owner.** Engineering, with Lending Ops on who reviews wording before it is sent — the review step
is the control, not the drafting.

**When.** After G1, or W10 as the plan's Path B reach if G1 slips. Worth putting to the client
rather than assuming: the plan wants it ahead of her timeline, and she has not been asked.

### G2 is split, and half of it is in this cycle after all

The second dependency above — policy retrieval that answers — is the plan's own **driver**
(`docs/plan-weeks7-10.md` §2 requires RAG working end to end on `main`), not just an input to
drafted wording. Deferring it whole means the W10 handover cannot claim RAG works, which is the
bar the weeks 7–10 arc is assessed against. So the retrieval half is split:

**Both halves are now BUILT and on `main`** — G2a as PR #56 (`fb1fc66`) and G2b as PR #64
(`609605b`), both 2026-08-23. Cite the merge commits, not the branch that carried them. The
scoping below stands as written except where marked; the corrections are kept rather than
edited away, because two of them are the reason the estimate was wrong.

- **G2a — the import seam. This cycle, 1–2 days. BUILT.** `rag_eval/` is repo-root, origination
  imports are `app.*` relative to the service directory, and there is no repo-wide venv, so
  `import rag_eval` does not resolve in the container. Closing that is the expensive half of
  retrieval and it stands alone as a commit. Do not fix it by copying modules into the service —
  that reproduces the per-service redactor duplication `redactor-drift` exists to police.
  **Correction:** the cause is narrower than "no venv". Origination's build context was
  `./services/origination-service`, so the package was outside the context and never entered
  the image at all. The close is a repo-root context plus a `COPY`, no path plumbing. The
  corpus needed nothing — `docker-compose.yml` has bind-mounted `./policies` read-only into
  this service since the initial scaffold.
- **G2b — `search_policy` on the assistant loop, plus the drafted wording. Next cycle. BUILT
  (the retrieval half; the drafted wording stays with G1).** A third entry in `_TOOLS`
  (`services/origination-service/app/assistant.py`), backed by `rag_eval.index`, declared in
  `app/prompts/decision_assistant.py`. Needs **ADR 0019** (0014 through 0018 are taken — this
  card said 0016 when 0016 and 0017 were still free, and 0018 went to the
  double-charge interim ADR, which merges first) and two design constraints settled:
  retrieval abstains below the score threshold, and is excluded from the `task="decision"`
  path entirely so generation never reaches the causal path. Both are built as specified.
  **Correction, and this is why 0.5–1 day was wrong:** loop dispatch DID need changing — it
  passes only the application id to every tool, and `search_policy` takes a model-supplied
  query. And the export contract refuses free text in history turns
  (`app/llm/request_builder.py:236-238` masks any whitespace-bearing string), so retrieved
  policy prose cannot reach the model at all. ADR 0019 resolves that by keeping the text on
  the officer's side: the model picks the query, code quotes the corpus chunk verbatim.

**Why split rather than defer or build whole.** Building all of it costs 4–6 days against a cycle
that already owes week-7 reconciliation (6–8) and the lost-update fix; nothing fits. Deferring all
of it leaves a week-long job that never finds a gap to fit into. The seam alone turns the rest into
a day's work whenever one appears, and no client is waiting on either half — this is a decision
about the plan's own bar, not about delivery to Lending Ops.

---

## Not carded, still open

- **What the back office actually exports from.** The client says someone exports the decision; no
  export endpoint, report, or file writer exists anywhere under `services/` for decisions. We are
  proceeding on the assumption that the back office reads the officer screen or queries
  `decision_events.principal_reasons` directly, both of which carry all four reasons — the first
  once PR #34 lands. **That is an assumption, not a verified fact**, and it is the one thing in
  this cycle's scope that could turn out to need code. If a real export artifact exists outside
  this repo, it becomes a card.
