"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import StatusChip from "../../../components/StatusChip";
import { apiGet, apiPost } from "../../../lib/api";
import { usd, pct, shortDate } from "../../../lib/format";

interface Kyc {
  name_verified?: boolean;
  dob_verified?: boolean;
  address_verified?: boolean;
  ssn_verified?: boolean;
}

interface Offer {
  apr: number;
  finance_charge: number;
  monthly_payment: number;
  amount_financed: number;
  total_of_payments: number;
}

interface Applicant {
  id?: number;
  name?: string;
  email?: string;
  phone?: string;
  address?: string;
  is_entity?: boolean;
}

interface Application {
  id: string | number;
  // The detail endpoint returns `applicant` as a nested object; the list
  // endpoint returns a flat `applicant_name` string. Support both.
  applicant?: Applicant | string;
  applicant_name?: string;
  amount: number;
  term_months: number;
  purpose: string;
  status: string;
  employer?: string;
  job_title?: string;
  created_at?: string;
  kyc?: Kyc;
  decision?: string;
  offer?: Offer;
}

// One walk of the provenance chain, as v_disclosure_provenance returns it (ADR 0012).
// Every field is optional because a legacy offer genuinely has a partial chain — the view
// reports that rather than hiding it, and so does this screen.
interface Provenance {
  disclosure_id?: number | null;
  disclosure_status?: string | null;
  disclosed_apr?: string | null;
  fee_schedule_version?: string | null;
  apr_method_version?: string | null;
  content_fingerprint?: string | null;
  delivered_at?: string | null;
  offer_id?: number | null;
  decision_event_id?: number | null;
  decision_outcome?: string | null;
  application_id?: number | null;
  applicant_id?: number | null;
  chain_complete?: boolean;
  missing_edges?: string[];
}

// The stored borrower-facing document, as disclosure-service persisted it alongside the
// record. The figures are strings because they are the exact spellings the record was
// checked against — reparsing them as numbers here would reintroduce the rounding the
// minor-unit columns exist to avoid.
interface DisclosureDocument {
  heading: string;
  figures: {
    apr: string;
    finance_charge: string;
    amount_financed: string;
    total_of_payments: string;
    monthly_payment: string;
  };
  payment_terms: string;
  prepayment: string;
}

// Labelled and grouped, per Reg Z 1026.17(a): the officer approving this has to see the
// figures the way the borrower will, not a fingerprint standing in for them.
const DOCUMENT_FIGURE_LABELS: [keyof DisclosureDocument["figures"], string][] = [
  ["apr", "Annual Percentage Rate"],
  ["finance_charge", "Finance Charge"],
  ["amount_financed", "Amount Financed"],
  ["total_of_payments", "Total of Payments"],
  ["monthly_payment", "Monthly Payment"],
];

const REJECT_REASONS = [
  { value: "wording", label: "Wording — send back to the assembler" },
  { value: "formatting", label: "Formatting — send back to the assembler" },
  { value: "wrong_terms", label: "Wrong terms — back to decisioning" },
  { value: "wrong_rate", label: "Wrong rate — back to decisioning" },
  { value: "ineligible", label: "Not eligible — back to decisioning" },
];

interface DecisionResult {
  app_id: string | number;
  decision: string;
  score?: number;
  adverse_action_reason?: string;
}

interface AssistantReason {
  code: string;
  reason: string;
}

interface AssistantResult {
  application_id: string | number;
  record_status?: string;
  outcome?: string;
  // Model score from the persisted decision record (null on legacy records that never
  // captured drivers) -- same fact the manual Run decision panel shows.
  score?: number;
  policy_band?: string;
  principal_reasons?: AssistantReason[];
  decided_by?: string;
  decided_at?: string;
  summary?: string;
  narration_validated?: boolean;
}

// Officer triage summary from the loan-summary LLM prompt (GET /applications/{id}/summary).
// Advisory only: recommended_next_step is a triage hint, not the record-backed decision.
interface Summary {
  summary: string;
  risk_flags: string[];
  recommended_next_step: string;
}

const OFFER_RATE_PCT = 7.99;

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

function prettyPurpose(p?: string): string {
  return (p || "")
    .replace(/_/g, " ")
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

export default function UnderwritingDetailPage() {
  const params = useParams<{ appId: string }>();
  const appId = params?.appId;

  const [app, setApp] = useState<Application | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  // action state (mirrors the servicing detail action pattern)
  const [decision, setDecision] = useState<DecisionResult | null>(null);
  const [offer, setOffer] = useState<Offer | null>(null);
  const [boardedLoanId, setBoardedLoanId] = useState<string | number | null>(
    null
  );
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [assistant, setAssistant] = useState<AssistantResult | null>(null);
  const [summary, setSummary] = useState<Summary | null>(null);
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  // Not named `document`: that shadows the DOM global inside this component.
  const [disclosureDoc, setDisclosureDoc] = useState<DisclosureDocument | null>(
    null
  );
  const [rejectReason, setRejectReason] = useState(REJECT_REASONS[0].value);

  // Route generation. Next.js reuses this page component across /underwriting/[appId]
  // navigations, so a request can outlive the application it was fired from. Each handler
  // captures this counter when it starts and compares on completion: anything from before
  // the last navigation is dropped. Generations rather than appIds, so navigating away and
  // BACK to the same application still discards the earlier in-flight call.
  const routeGenRef = useRef(0);

  // Idempotency key for the officer decision action. Generated once per attempt on one
  // page and reused across retries (a timeout retry or second click replays the recorded
  // decision instead of re-pulling credit and appending a second regulated event —
  // parity with the borrower path, PR review). Not derived from appId: an officer may
  // deliberately re-decide in a fresh session (page reload = new key = new decision),
  // whereas a borrower's post-submit inputs never change, so their key is stable.
  const decisionKeyRef = useRef<string | null>(null);

  // AI decisioning assistant idempotency key (ADR 0009 §5). Held only across retries of a
  // single in-flight attempt and rotated after a confirmed success, so a later intentional
  // run re-scores current state instead of replaying the recorded event.
  const assistantKeyRef = useRef<string | null>(null);

  // Per-application state reset. Every value below describes ONE application, so without
  // this the previous applicant's name, contact details, KYC rows, decision, assistant
  // card, offer, boarded loan id and disclosure provenance stay on the next applicant's
  // screen (PR review). The provenance block is the sharpest case: it renders a disclosed
  // APR, a content fingerprint and the full disclosure -> offer -> decision -> applicant
  // chain, so a stale one attributes one applicant's regulated disclosure to another.
  // The idempotency keys reset with them: a key identifies one attempt on one page.
  //
  // This runs DURING render, not in an effect. A passive effect runs after the browser
  // paints, so the first commit for the new appId would pair the previous applicant's
  // regulated facts with the new application number in the header before the reset ever
  // fired — a real frame on screen, invisible to any assertion made after effects flush
  // (PR review). Adjusting state during render makes React re-run this component with the
  // cleared state and commit only that, so no frame shows one applicant under another's
  // id. Placed above the load effect, so the generation is bumped before load captures it.
  const [routeAppId, setRouteAppId] = useState(appId);
  if (appId !== routeAppId) {
    setRouteAppId(appId);
    routeGenRef.current += 1;
    setApp(null);
    setLoading(true);
    setError(null);
    setDecision(null);
    setAssistant(null);
    setSummary(null);
    setOffer(null);
    setBoardedLoanId(null);
    setProvenance(null);
    setDisclosureDoc(null);
    setRejectReason(REJECT_REASONS[0].value);
    setActionMsg(null);
    setActionErr(null);
    setActionBusy(false);
    decisionKeyRef.current = null;
    assistantKeyRef.current = null;
  }

  // A 404 here means "no disclosure yet", which is the normal state before the pipeline
  // has run — not an error worth showing. Guarded by the route generation like every other
  // async handler on this page: a disclosure fetch fired for the previous applicant must
  // not land on this one's screen.
  // `gen` defaults to the current generation for the load effect, but a caller that started
  // before a route change passes the generation it captured THEN — otherwise this guard reads
  // the new route's generation (capturing at call time is too late) and writes the previous
  // applicant's provenance/document onto the screen now showing someone else.
  const loadDisclosure = useCallback(async (gen: number = routeGenRef.current) => {
    if (!appId) return;
    try {
      const p = (await apiGet(
        `/los/applications/${appId}/disclosure`
      )) as Provenance;
      if (routeGenRef.current !== gen) return;
      setProvenance(p);
    } catch {
      if (routeGenRef.current !== gen) return;
      setProvenance(null);
    }
    // Fetched here rather than read off the generate response, because the officer who
    // approves or delivers is a different session from the one that generated: on a page
    // load, or under maker-checker a different person entirely. Reading it only from the
    // POST reply meant the reviewer approved a document they could not open.
    //
    // Separate try, and a 404 is the normal "none recorded" (every row written before
    // migration 0012 has none) — a missing document must not blank out the provenance
    // block, which is a different fact about a different question.
    try {
      const d = (await apiGet(
        `/los/applications/${appId}/disclosure/document`
      )) as DisclosureDocument;
      if (routeGenRef.current !== gen) return;
      setDisclosureDoc(d);
    } catch {
      if (routeGenRef.current !== gen) return;
      setDisclosureDoc(null);
    }
  }, [appId]);

  const load = useCallback(async () => {
    if (!appId) return;
    const gen = routeGenRef.current;
    setLoading(true);
    setError(null);
    try {
      const a = (await apiGet(`/los/applications/${appId}`)) as Application;
      if (routeGenRef.current !== gen) return;
      setApp(a);
      if (a.offer) setOffer(a.offer);
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setError(errMsg(err, "Could not load this application."));
      setApp(null);
    } finally {
      if (routeGenRef.current === gen) setLoading(false);
    }
  }, [appId]);

  useEffect(() => {
    load();
    loadDisclosure();
  }, [load, loadDisclosure]);

  async function runDecision() {
    if (!appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    if (!decisionKeyRef.current) decisionKeyRef.current = crypto.randomUUID();
    try {
      const res = (await apiPost(
        `/los/applications/${appId}/decision`,
        undefined,
        { "Idempotency-Key": decisionKeyRef.current }
      )) as DecisionResult;
      if (routeGenRef.current !== gen) return;
      setDecision(res);
      setApp((prev) => (prev ? { ...prev, decision: res.decision } : prev));
      // A manual decision supersedes whatever the assistant last reported: that card's
      // summary, principal reasons, decided_at and outcome describe an EARLIER decision
      // event. Leaving it would put two record-backed but conflicting outcomes on one
      // screen. Explain repopulates it from the current record on demand (PR review).
      setAssistant(null);
      setActionMsg(`Decision recorded: ${res.decision}.`);
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "Could not run a decision."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  // KYC recovery (ADR 0011): an application submitted while kyc-service was down has no
  // passing kyc_checks row, so the mandatory gate 409s Run decision / Make offer / Accept.
  // Re-run KYC for this application (officer is authorized by session) and reload to show
  // the refreshed result -- the operational counterpart to the borrower's retry.
  async function recheckKyc() {
    if (!appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      await apiPost(`/los/applications/${appId}/recheck-kyc`, undefined);
      await load();
      if (routeGenRef.current !== gen) return;
      setActionMsg("Identity verification re-run.");
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "Could not re-run identity verification."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function makeOffer() {
    if (!app || !appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost("/los/offer", {
        app_id: appId,
        principal: app.amount,
        annual_rate_pct: OFFER_RATE_PCT,
        term_months: app.term_months,
      })) as { app_id: string | number; disclosure?: Offer; offer?: Offer };
      if (routeGenRef.current !== gen) return;
      const disc = res.disclosure ?? res.offer ?? null;
      setOffer(disc);
      setActionMsg("Offer generated.");
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "Could not generate an offer."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  // AI decisioning assistant (ADR 0009 §5). "Run" drives the agent loop: its score
  // tool performs the SAME regulated decision + append-only record as Run decision, then
  // the model narrates the recorded outcome (narration validated against the record —
  // recorded facts win). "Explain" is read-only and never re-scores. 503 = LLM feature
  // off or provider unavailable. The idempotency key it uses is assistantKeyRef, declared
  // with the other per-application state above.
  //
  // An assistant response must describe the application it was requested for. Checked
  // before ANY state write, the assistant card included: a mismatched application_id means
  // the record belongs to another applicant and can never be shown on this screen.
  function isForApplication(res: AssistantResult, expectedAppId: string): boolean {
    return String(res.application_id) === String(expectedAppId);
  }

  // One mapping from an assistant result onto the primary decision panel, used by BOTH
  // Run and Explain. Both report the same append-only decision record, so the panel must
  // show the same record-derived facts either way; a single copy is what keeps the two
  // paths from drifting apart on regulated fields. Everything here is record-derived
  // (recorded facts win, ADR 0009 §5). Reason parity: DecisionOut.reason is the FIRST
  // principal reason; score parity: DecisionOut.score is an int. Returns false when the
  // response carries no recorded outcome so the caller can fail closed rather than
  // half-update the panel.
  function applyRecordedDecision(res: AssistantResult): boolean {
    const outcome = res.outcome;
    if (!outcome) return false;
    setApp((prev) => (prev ? { ...prev, decision: outcome } : prev));
    setDecision({
      app_id: res.application_id,
      decision: outcome,
      score: typeof res.score === "number" ? Math.round(res.score) : undefined,
      adverse_action_reason: res.principal_reasons?.[0]?.reason,
    });
    return true;
  }

  async function runAssistant() {
    if (!appId) return;
    const requestAppId = appId;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    if (!assistantKeyRef.current) assistantKeyRef.current = crypto.randomUUID();
    try {
      const res = (await apiPost(
        `/los/assistant/decisions/${appId}`,
        undefined,
        { "Idempotency-Key": assistantKeyRef.current }
      )) as AssistantResult;
      if (routeGenRef.current !== gen) return;
      if (!isForApplication(res, requestAppId)) {
        setActionErr(
          "The AI assistant returned a result for a different application — the decision panel was not updated."
        );
        return;
      }
      setAssistant(res);
      // Fail closed on a 200 that carries no recorded outcome: an assistant run IS a
      // regulated decision, so a response missing the outcome is not a success. Leave the
      // existing decision state untouched, report the failure, and keep the idempotency
      // key so a retry replays that attempt instead of recording a second event.
      // (PR #11 review; the server already refuses an unrecorded decision, this is the
      // client-side backstop for a drifted contract or a proxy-mangled body.)
      //
      // On success this REPLACES the standard-decision state with THIS run's recorded
      // facts rather than clearing it: blanking them hid fields officers rely on, while
      // leaving them would show a PRIOR run's data beside a fresh outcome.
      if (!applyRecordedDecision(res)) {
        setActionErr(
          "The AI assistant returned no recorded outcome — the decision panel was not updated."
        );
        return;
      }
      // Rotate the idempotency key after a confirmed success so a later intentional
      // "Run AI assistant" click re-scores current state rather than replaying this
      // recorded event. A failed run leaves the key set (the catch below does not
      // reset it) so a retry of the same attempt still replays, not double-records.
      assistantKeyRef.current = null;
      setActionMsg("AI assistant ran the decision.");
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "The AI assistant is unavailable."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  // Officer triage summary. Button-triggered ONLY — deliberately NOT in the on-mount
  // load effect: a GET here is a paid provider call, so summarizing on every application
  // open would bill each visit. Guarded by the route generation like every async handler:
  // capture `gen` before the first await and drop a response that lands after navigation,
  // so applicant A's summary never appears under applicant B's header.
  async function runSummary() {
    if (!appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiGet(
        `/los/applications/${appId}/summary`
      )) as Summary;
      if (routeGenRef.current !== gen) return;
      setSummary(res);
      setActionMsg("Summary generated.");
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "The summary is unavailable."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function explainAssistant() {
    if (!appId) return;
    const requestAppId = appId;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiGet(
        `/los/assistant/decisions/${appId}`
      )) as AssistantResult;
      if (routeGenRef.current !== gen) return;
      if (!isForApplication(res, requestAppId)) {
        setActionErr(
          "The AI assistant returned a result for a different application — the decision panel was not updated."
        );
        return;
      }
      setAssistant(res);
      // Explain is read-only but NOT stale-safe on its own: the GET reports the CURRENT
      // recorded decision (assistant.py _validated_final fetches the record unscoped for
      // this task), so it can legitimately return a newer outcome than whatever this tab
      // last put in the panel -- e.g. an officer's earlier Run decision here, another
      // decision event recorded elsewhere, then Explain. Without this sync the assistant
      // card would show the latest recorded outcome while the primary panel kept the old
      // score and adverse-action reason: a user-visible contradiction on regulated
      // decision facts. Same fail-closed rule as Run -- no recorded outcome means the
      // panel is left alone and the officer is told, never half-updated (PR review).
      if (!applyRecordedDecision(res)) {
        setActionErr(
          "The AI assistant returned no recorded outcome — the decision panel was not updated."
        );
        return;
      }
      setActionMsg("AI assistant explained the recorded decision (no re-score).");
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "The AI assistant is unavailable."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function generateDisclosure() {
    if (!appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      await apiPost(`/los/applications/${appId}/disclosure`);
      if (routeGenRef.current !== gen) return;
      await loadDisclosure(gen);
      if (routeGenRef.current !== gen) return;
      setActionMsg("Disclosure generated and held for compliance review.");
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      // A 422 here is the verification gate refusing the document, not an outage. The
      // reason it carries is the whole point of the gate, so it is shown verbatim.
      setActionErr(errMsg(err, "Could not generate a disclosure."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function transitionDisclosure(toStatus: string, reasonCode?: string) {
    if (!appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(
        `/los/applications/${appId}/disclosure/transition`,
        { to_status: toStatus, reason_code: reasonCode ?? null }
      )) as { status?: string; routed_to?: string | null };
      if (routeGenRef.current !== gen) return;
      await loadDisclosure(gen);
      if (routeGenRef.current !== gen) return;
      setActionMsg(
        res.routed_to
          ? `Disclosure rejected; the work goes back to ${res.routed_to}.`
          : `Disclosure is now ${String(res.status || toStatus).replace(/_/g, " ")}.`
      );
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "Could not update the disclosure."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function acceptAndBoard() {
    if (!appId) return;
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(`/los/applications/${appId}/accept`)) as {
        loan_id: string | number;
      };
      if (routeGenRef.current !== gen) return;
      setBoardedLoanId(res.loan_id);
      setActionMsg(`Boarded to servicing as loan #${String(res.loan_id)}.`);
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "Could not accept and board this application."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  if (loading && !app) {
    return (
      <main className="wrap">
        <p className="muted">Loading application #{appId}…</p>
      </main>
    );
  }

  if (error && !app) {
    return (
      <main className="wrap">
        <p>
          <Link href="/underwriting">← Back to underwriting</Link>
        </p>
        <div className="alert alert-error">{error}</div>
      </main>
    );
  }

  const applicantObj =
    app && typeof app.applicant === "object" ? app.applicant : null;
  const applicantName =
    applicantObj?.name ||
    app?.applicant_name ||
    (typeof app?.applicant === "string" ? app.applicant : "") ||
    "Applicant";
  const currentDecision = decision?.decision || app?.decision || null;
  const disclosureStatus = provenance?.disclosure_status || null;
  // Boarding is consummation (Reg Z 1026.17(b)), so it waits on delivery. Absent
  // provenance means no disclosure exists yet, which is emphatically not delivered.
  const disclosureDelivered = disclosureStatus === "delivered";
  // A disclosure that exists but has no stored document is repairable from this screen:
  // re-running generation reaches disclosure-service's replay, which records a document on
  // a row that has none (checked against the figures already on that row). Not offered for
  // a DELIVERED row — trg_disclosures_freeze_delivered rejects any UPDATE of it, so that
  // one genuinely needs an operator. Without this the officer had no action at all: the
  // button was disabled the moment any disclosure existed, while delivery stayed refused
  // for want of the document, so the application could not move in either direction.
  const disclosureNeedsDocument =
    !!disclosureStatus && !disclosureDoc && !disclosureDelivered;
  // Delivery is necessary but not sufficient: a row delivered before document recording
  // carries the flag and the timestamp with no document, and the accept route refuses that
  // pair rather than funding a loan whose disclosure cannot be read. Mirrored here so the
  // officer does not meet the refusal as a 409 on the terminal action — same reason the
  // deliver control already gates on the document.
  const boardable = disclosureDelivered && !!disclosureDoc;

  return (
    <main className="wrap">
      <p style={{ marginBottom: 12 }}>
        <Link href="/underwriting">← Back to underwriting</Link>
      </p>

      {/* Header */}
      <div className="spread">
        <div>
          <h1 style={{ marginBottom: 6 }}>{applicantName}</h1>
          <p className="sub" style={{ margin: 0 }}>
            Application #{String(appId)}
          </p>
        </div>
        {app ? <StatusChip status={app.status} /> : null}
      </div>

      {/* Request summary */}
      <div className="grid grid-3" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Requested amount</div>
          <div className="kpi-value">{usd(app?.amount)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Term</div>
          <div className="kpi-value" style={{ fontSize: 20 }}>
            {app?.term_months} months
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Received</div>
          <div className="kpi-value" style={{ fontSize: 20 }}>
            {shortDate(app?.created_at)}
          </div>
        </div>
      </div>

      {/* Applicant detail */}
      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>
          Applicant
        </div>
        <div className="dl">
          <div className="dl-row">
            <dt>Name</dt>
            <dd>{applicantName}</dd>
          </div>
          <div className="dl-row">
            <dt>Type</dt>
            <dd>{applicantObj?.is_entity ? "Entity / business" : "Individual"}</dd>
          </div>
          <div className="dl-row">
            <dt>Email</dt>
            <dd>{applicantObj?.email || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Phone</dt>
            <dd>{applicantObj?.phone || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Address</dt>
            <dd>{applicantObj?.address || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Purpose</dt>
            <dd>{prettyPurpose(app?.purpose)}</dd>
          </div>
          <div className="dl-row">
            <dt>Employer</dt>
            <dd>{app?.employer || "—"}</dd>
          </div>
          <div className="dl-row">
            <dt>Job title</dt>
            <dd>{app?.job_title || "—"}</dd>
          </div>
        </div>
      </div>

      {/* KYC */}
      <h2>Identity verification (KYC)</h2>
      <div className="card">
        <div className="dl">
          <KycRow label="Name" ok={app?.kyc?.name_verified} />
          <KycRow label="Date of birth" ok={app?.kyc?.dob_verified} />
          <KycRow label="Address" ok={app?.kyc?.address_verified} />
          <KycRow label="SSN" ok={app?.kyc?.ssn_verified} />
        </div>
        <p className="hint" style={{ marginTop: 12 }}>
          If identity verification was unavailable at submit, decision/offer/accept
          are blocked until it is re-run.
        </p>
        <button
          className="secondary"
          onClick={recheckKyc}
          disabled={actionBusy}
        >
          {actionBusy ? "Re-checking…" : "Re-run identity check"}
        </button>
      </div>

      {/* Action feedback (shared by all panels) */}
      {actionMsg ? <div className="alert alert-success">{actionMsg}</div> : null}
      {actionErr ? <div className="alert alert-error">{actionErr}</div> : null}

      {/* Application summary — advisory triage, read before deciding. Above the
          Decision panel on purpose, and visually distinct from it: the
          recommended_next_step is a triage hint, not a second decision outcome. */}
      <h2>Application summary</h2>
      <div className="card">
        <div className="spread">
          <div>
            <div className="card-title" style={{ marginBottom: 8 }}>
              LLM triage summary
            </div>
            <p className="hint" style={{ margin: 0 }}>
              An advisory summary of this application for triage. It reads only the
              application facts (never applicant identity) and does not record anything.
            </p>
          </div>
          <button onClick={runSummary} disabled={actionBusy}>
            {actionBusy ? "Working…" : "Summarize"}
          </button>
        </div>
        {summary ? (
          <div style={{ marginTop: 12 }}>
            <p style={{ margin: 0 }}>{summary.summary}</p>
            {summary.risk_flags.length > 0 ? (
              <div
                style={{
                  display: "flex",
                  flexWrap: "wrap",
                  gap: 6,
                  marginTop: 10,
                }}
              >
                {summary.risk_flags.map((flag, i) => (
                  <span key={i} className="chip chip-amber">
                    {flag}
                  </span>
                ))}
              </div>
            ) : null}
            <p className="hint" style={{ marginTop: 10 }}>
              Triage hint from the assistant, not a lending decision:{" "}
              <strong>{summary.recommended_next_step}</strong>
            </p>
          </div>
        ) : null}
      </div>

      {/* Decision */}
      <h2>Decision</h2>
      <div className="card">
        <div className="spread">
          <div>
            <div className="card-title" style={{ marginBottom: 8 }}>
              Underwriting decision
            </div>
            {currentDecision ? (
              <StatusChip status={currentDecision} />
            ) : (
              <span className="muted">No decision yet.</span>
            )}
            {typeof decision?.score === "number" ? (
              <p className="hint" style={{ marginTop: 10 }}>
                Model score: {decision.score}
              </p>
            ) : null}
            {decision?.adverse_action_reason ? (
              <div className="alert alert-warn">
                <strong>Adverse action reason:</strong>{" "}
                {decision.adverse_action_reason}
              </div>
            ) : null}
          </div>
          <button onClick={runDecision} disabled={actionBusy}>
            {actionBusy ? "Working…" : "Run decision"}
          </button>
        </div>
      </div>

      {/* AI decisioning assistant */}
      <h2>AI decisioning assistant</h2>
      <div className="card">
        <div className="spread">
          <div>
            <div className="card-title" style={{ marginBottom: 8 }}>
              LLM assistant
            </div>
            <p className="hint" style={{ margin: 0 }}>
              The assistant scores through the deterministic model tool, then narrates
              the recorded outcome. The LLM never sets the score.
            </p>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <button onClick={runAssistant} disabled={actionBusy}>
              {actionBusy ? "Working…" : "Run AI assistant"}
            </button>
            <button
              className="btn-ghost"
              onClick={explainAssistant}
              disabled={actionBusy}
            >
              {actionBusy ? "Working…" : "Explain"}
            </button>
          </div>
        </div>

        {assistant ? (
          <div style={{ marginTop: 16 }}>
            <div className="spread" style={{ marginBottom: 10 }}>
              {assistant.outcome ? (
                <StatusChip status={assistant.outcome} />
              ) : (
                <span className="muted">No recorded decision.</span>
              )}
              {assistant.narration_validated ? (
                <span className="chip chip-green">
                  ✓ narration validated against record
                </span>
              ) : (
                <span className="chip chip-amber">
                  ⚠ narration diverged — showing recorded facts
                </span>
              )}
            </div>
            {assistant.summary ? (
              <p style={{ marginTop: 0 }}>{assistant.summary}</p>
            ) : null}
            {assistant.principal_reasons &&
            assistant.principal_reasons.length > 0 ? (
              <ul className="hint" style={{ marginTop: 8 }}>
                {assistant.principal_reasons.map((r) => (
                  <li key={r.code}>
                    <strong>{r.code}</strong>: {r.reason}
                  </li>
                ))}
              </ul>
            ) : null}
            {assistant.decided_by || assistant.decided_at ? (
              <p className="hint" style={{ marginTop: 8 }}>
                Recorded by {assistant.decided_by || "—"}
                {assistant.decided_at
                  ? ` at ${shortDate(assistant.decided_at)}`
                  : ""}
                {assistant.policy_band
                  ? ` · policy band ${assistant.policy_band}`
                  : ""}
              </p>
            ) : null}
          </div>
        ) : null}
      </div>

      {/* Offer */}
      <h2>Offer</h2>
      <div className="card">
        <div className="spread" style={{ marginBottom: offer ? 16 : 0 }}>
          <p className="hint" style={{ margin: 0 }}>
            Generate a Truth-in-Lending offer at {pct(OFFER_RATE_PCT)} APR for{" "}
            {usd(app?.amount)} over {app?.term_months} months.
          </p>
          <button
            className="btn-ghost"
            onClick={makeOffer}
            disabled={actionBusy}
          >
            {actionBusy ? "Working…" : offer ? "Regenerate offer" : "Make offer"}
          </button>
        </div>

        {offer ? (
          <div className="tila">
            <div className="tila-title">Federal Truth-in-Lending Disclosure</div>
            <div className="tila-grid">
              <div className="tila-cell tila-cell-apr">
                <div className="tila-cell-label">Annual Percentage Rate</div>
                <div className="tila-cell-desc">
                  The cost of your credit as a yearly rate.
                </div>
                <div className="tila-cell-value">{pct(offer.apr)}</div>
              </div>
              <div className="tila-cell">
                <div className="tila-cell-label">Finance Charge</div>
                <div className="tila-cell-desc">
                  The dollar amount the credit will cost.
                </div>
                <div className="tila-cell-value">
                  {usd(offer.finance_charge)}
                </div>
              </div>
              <div className="tila-cell">
                <div className="tila-cell-label">Amount Financed</div>
                <div className="tila-cell-desc">
                  The amount of credit provided.
                </div>
                <div className="tila-cell-value">
                  {usd(offer.amount_financed)}
                </div>
              </div>
              <div className="tila-cell">
                <div className="tila-cell-label">Total of Payments</div>
                <div className="tila-cell-desc">
                  What will be paid after all payments are made.
                </div>
                <div className="tila-cell-value">
                  {usd(offer.total_of_payments)}
                </div>
              </div>
            </div>
          </div>
        ) : null}
      </div>

      {/* TILA disclosure — generation, compliance hold, delivery (spec D4/D6/D9) */}
      <h2>TILA disclosure</h2>
      <div className="card">
        <div className="spread" style={{ marginBottom: 16 }}>
          <div>
            <div className="card-title" style={{ marginBottom: 8 }}>
              Document status
            </div>
            {disclosureStatus ? (
              <StatusChip status={disclosureStatus} />
            ) : (
              <span className="muted">Not generated yet.</span>
            )}
            {provenance?.delivered_at ? (
              <p className="hint" style={{ marginTop: 10 }}>
                Delivered {shortDate(provenance.delivered_at)}.
              </p>
            ) : null}
          </div>
          <button
            className="btn-ghost"
            onClick={generateDisclosure}
            disabled={
              actionBusy || (!!disclosureStatus && !disclosureNeedsDocument)
            }
            title={
              disclosureNeedsDocument
                ? "Re-run generation to record the missing document on this disclosure."
                : disclosureStatus
                  ? "A disclosure already exists for this application."
                  : undefined
            }
          >
            {actionBusy
              ? "Working…"
              : disclosureNeedsDocument
                ? "Record missing document"
                : "Generate disclosure"}
          </button>
        </div>

        {provenance && provenance.chain_complete === false ? (
          <div className="alert alert-warn">
            <strong>Incomplete provenance chain.</strong> Missing:{" "}
            {(provenance.missing_edges || []).join(", ") || "unknown"}. This
            disclosure cannot be traced end to end and must not be delivered.
          </div>
        ) : null}

        {provenance?.disclosure_id ? (
          <>
            {/* The document itself, above the review controls — approving or delivering is
                a judgement about this content, and it used to be unreachable from this
                screen. Figures are printed as stored, never reformatted: they are the exact
                spellings disclosure-service checked against the minor-unit record. */}
            {disclosureDoc ? (
              <div className="tila" style={{ marginBottom: 16 }}>
                <div className="tila-title">{disclosureDoc.heading}</div>
                <div className="tila-grid">
                  {DOCUMENT_FIGURE_LABELS.map(([field, label]) => (
                    <div
                      key={field}
                      className={
                        field === "apr" ? "tila-cell tila-cell-apr" : "tila-cell"
                      }
                    >
                      <div className="tila-cell-label">{label}</div>
                      <div className="tila-cell-value">
                        {field === "apr"
                          ? `${disclosureDoc.figures.apr}%`
                          : `$${disclosureDoc.figures[field]}`}
                      </div>
                    </div>
                  ))}
                </div>
                <p className="hint" style={{ marginTop: 12 }}>
                  {disclosureDoc.payment_terms}
                </p>
                <p className="hint" style={{ marginTop: 8 }}>
                  {disclosureDoc.prepayment}
                </p>
              </div>
            ) : disclosureNeedsDocument ? (
              // Names the action that fixes it, because there now is one: the
              // replay in disclosure-service records a document on a row that
              // has none, so re-running generation repairs this row in place
              // rather than minting a second regulated record.
              <div className="alert alert-warn">
                <strong>No document recorded.</strong> This disclosure predates
                document recording, so there is nothing to review and delivery
                will be refused. Use{" "}
                <strong>Record missing document</strong> above to re-run
                generation and store it against this disclosure.
              </div>
            ) : (
              // Delivered with no document: frozen by
              // trg_disclosures_freeze_delivered, so no officer action reaches
              // it and the honest answer is that it needs an operator.
              <div className="alert alert-warn">
                <strong>No document recorded.</strong> This disclosure was
                delivered before document recording and is frozen, so the
                document cannot be added and boarding will be refused. It needs
                an operator.
              </div>
            )}

            <div className="dl">
              <div className="dl-row">
                <dt>Disclosed APR</dt>
                <dd>
                  {provenance.disclosed_apr
                    ? `${provenance.disclosed_apr}%`
                    : "—"}
                </dd>
              </div>
              <div className="dl-row">
                <dt>Provenance</dt>
                <dd>
                  disclosure #{provenance.disclosure_id} → offer #
                  {provenance.offer_id ?? "—"} → decision event #
                  {provenance.decision_event_id ?? "—"} → application #
                  {provenance.application_id ?? "—"} → applicant #
                  {provenance.applicant_id ?? "—"}
                </dd>
              </div>
              <div className="dl-row">
                <dt>Rules applied</dt>
                <dd>
                  fees {provenance.fee_schedule_version || "—"} · APR{" "}
                  {provenance.apr_method_version || "—"}
                </dd>
              </div>
              <div className="dl-row">
                <dt>Fingerprint</dt>
                <dd>
                  <code>{provenance.content_fingerprint || "—"}</code>
                </dd>
              </div>
            </div>

            <div
              style={{
                display: "flex",
                gap: 8,
                alignItems: "center",
                flexWrap: "wrap",
                marginTop: 16,
              }}
            >
              {disclosureStatus === "draft" ? (
                <button
                  onClick={() => transitionDisclosure("in_review")}
                  disabled={actionBusy}
                >
                  Send to compliance
                </button>
              ) : null}
              {disclosureStatus === "in_review" ? (
                <button
                  onClick={() => transitionDisclosure("approved")}
                  disabled={actionBusy}
                >
                  Approve
                </button>
              ) : null}
              {disclosureStatus === "approved" ? (
                <button
                  onClick={() => transitionDisclosure("delivered")}
                  disabled={
                    actionBusy ||
                    provenance.chain_complete === false ||
                    !disclosureDoc
                  }
                  title={
                    !disclosureDoc
                      ? "This disclosure has no recorded document to deliver."
                      : undefined
                  }
                >
                  Deliver to borrower
                </button>
              ) : null}
              {disclosureStatus === "in_review" ||
              disclosureStatus === "approved" ? (
                <>
                  <select
                    value={rejectReason}
                    onChange={(e) => setRejectReason(e.target.value)}
                    disabled={actionBusy}
                  >
                    {REJECT_REASONS.map((r) => (
                      <option key={r.value} value={r.value}>
                        {r.label}
                      </option>
                    ))}
                  </select>
                  <button
                    className="secondary"
                    onClick={() => transitionDisclosure("draft", rejectReason)}
                    disabled={actionBusy}
                  >
                    Reject
                  </button>
                </>
              ) : null}
              {disclosureStatus === "delivered" ? (
                <p className="hint" style={{ margin: 0 }}>
                  Delivered disclosures are frozen — the record of what the
                  borrower was shown cannot be edited.
                </p>
              ) : null}
            </div>
          </>
        ) : null}
      </div>

      {/* Accept & board */}
      <h2>Board to servicing</h2>
      <div className="card">
        {boardedLoanId ? (
          <div className="alert alert-success" style={{ margin: 0 }}>
            Boarded. Loan <strong>#{String(boardedLoanId)}</strong> created.{" "}
            <Link href={`/servicing/${boardedLoanId}`}>
              Open the loan account →
            </Link>
          </div>
        ) : (
          <div className="spread">
            <p className="hint" style={{ margin: 0 }}>
              {boardable
                ? "Accept the offer and board this application as a serviced loan."
                : disclosureDelivered
                  ? "This disclosure was delivered with no recorded document, so it cannot be read and the loan cannot be boarded. It needs an operator."
                  : "Deliver the TILA disclosure first — boarding is consummation, and the disclosure has to reach the borrower before it."}
            </p>
            {/* Cosmetic only. The server refuses to board without a delivered disclosure
                that has a recorded document (origination accept route); this just stops the
                officer from discovering that as a 409 on the terminal action. */}
            <button
              onClick={acceptAndBoard}
              disabled={actionBusy || !boardable}
              title={
                boardable
                  ? undefined
                  : disclosureDelivered
                    ? "The delivered disclosure has no recorded document, so this loan cannot be boarded."
                    : "The TILA disclosure must be delivered before this loan can be boarded."
              }
            >
              {actionBusy ? "Working…" : "Accept & board"}
            </button>
          </div>
        )}
      </div>
    </main>
  );
}

function KycRow({ label, ok }: { label: string; ok?: boolean }) {
  return (
    <div className="dl-row">
      <dt>{label}</dt>
      <dd>
        {ok ? (
          <span className="chip chip-green">Verified</span>
        ) : (
          <span className="chip chip-amber">Unverified</span>
        )}
      </dd>
    </div>
  );
}
