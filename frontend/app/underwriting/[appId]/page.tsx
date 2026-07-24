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
  policy_band?: string;
  principal_reasons?: AssistantReason[];
  decided_by?: string;
  decided_at?: string;
  summary?: string;
  narration_validated?: boolean;
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

  const load = useCallback(async () => {
    if (!appId) return;
    setLoading(true);
    setError(null);
    try {
      const a = (await apiGet(`/los/applications/${appId}`)) as Application;
      setApp(a);
      if (a.offer) setOffer(a.offer);
    } catch (err) {
      setError(errMsg(err, "Could not load this application."));
      setApp(null);
    } finally {
      setLoading(false);
    }
  }, [appId]);

  useEffect(() => {
    load();
  }, [load]);

  // Idempotency key for the officer decision action. Generated once per page mount
  // and reused across retries (a timeout retry or second click replays the recorded
  // decision instead of re-pulling credit and appending a second regulated event —
  // parity with the borrower path, PR review). Not derived from appId: an officer may
  // deliberately re-decide in a fresh session (page reload = new key = new decision),
  // whereas a borrower's post-submit inputs never change, so their key is stable.
  const decisionKeyRef = useRef<string | null>(null);

  async function runDecision() {
    if (!appId) return;
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
      setDecision(res);
      setApp((prev) => (prev ? { ...prev, decision: res.decision } : prev));
      setActionMsg(`Decision recorded: ${res.decision}.`);
    } catch (err) {
      setActionErr(errMsg(err, "Could not run a decision."));
    } finally {
      setActionBusy(false);
    }
  }

  // KYC recovery (ADR 0011): an application submitted while kyc-service was down has no
  // passing kyc_checks row, so the mandatory gate 409s Run decision / Make offer / Accept.
  // Re-run KYC for this application (officer is authorized by session) and reload to show
  // the refreshed result -- the operational counterpart to the borrower's retry.
  async function recheckKyc() {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      await apiPost(`/los/applications/${appId}/recheck-kyc`, undefined);
      await load();
      setActionMsg("Identity verification re-run.");
    } catch (err) {
      setActionErr(errMsg(err, "Could not re-run identity verification."));
    } finally {
      setActionBusy(false);
    }
  }

  async function makeOffer() {
    if (!app || !appId) return;
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
      const disc = res.disclosure ?? res.offer ?? null;
      setOffer(disc);
      setActionMsg("Offer generated.");
    } catch (err) {
      setActionErr(errMsg(err, "Could not generate an offer."));
    } finally {
      setActionBusy(false);
    }
  }

  // AI decisioning assistant (ADR 0009 §5). "Run" drives the agent loop: its score
  // tool performs the SAME regulated decision + append-only record as Run decision, then
  // the model narrates the recorded outcome (narration validated against the record —
  // recorded facts win). "Explain" is read-only and never re-scores. The idempotency
  // key is held only across retries of a single in-flight attempt and rotated after a
  // confirmed success, so a later intentional run re-scores current state instead of
  // replaying the recorded event. 503 = LLM feature off or provider unavailable.
  const assistantKeyRef = useRef<string | null>(null);

  async function runAssistant() {
    if (!appId) return;
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
      setAssistant(res);
      if (res.outcome) {
        setApp((prev) => (prev ? { ...prev, decision: res.outcome } : prev));
      }
      // Clear the standard-decision state on any successful run so the primary panel
      // can't render a prior run's score/adverse-action reason beside the assistant
      // result -- including a success that returns no outcome, which would otherwise
      // leave the stale score visible. The assistant endpoint returns no numeric
      // score, so there is nothing authoritative to repopulate; the assistant card
      // shows its own outcome and principal_reasons. (PR #11 review; compliance: no
      // stale regulated decision data on the officer screen.)
      setDecision(null);
      // Rotate the idempotency key after a confirmed success so a later intentional
      // "Run AI assistant" click re-scores current state rather than replaying this
      // recorded event. A failed run leaves the key set (the catch below does not
      // reset it) so a retry of the same attempt still replays, not double-records.
      assistantKeyRef.current = null;
      setActionMsg("AI assistant ran the decision.");
    } catch (err) {
      setActionErr(errMsg(err, "The AI assistant is unavailable."));
    } finally {
      setActionBusy(false);
    }
  }

  async function explainAssistant() {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiGet(
        `/los/assistant/decisions/${appId}`
      )) as AssistantResult;
      setAssistant(res);
      setActionMsg("AI assistant explained the recorded decision (no re-score).");
    } catch (err) {
      setActionErr(errMsg(err, "The AI assistant is unavailable."));
    } finally {
      setActionBusy(false);
    }
  }

  async function acceptAndBoard() {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(`/los/applications/${appId}/accept`)) as {
        loan_id: string | number;
      };
      setBoardedLoanId(res.loan_id);
      setActionMsg(`Boarded to servicing as loan #${String(res.loan_id)}.`);
    } catch (err) {
      setActionErr(errMsg(err, "Could not accept and board this application."));
    } finally {
      setActionBusy(false);
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
              Accept the offer and board this application as a serviced loan.
            </p>
            <button onClick={acceptAndBoard} disabled={actionBusy}>
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
