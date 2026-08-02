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
  const [provenance, setProvenance] = useState<Provenance | null>(null);
  const [rejectReason, setRejectReason] = useState(REJECT_REASONS[0].value);

  // A 404 here means "no disclosure yet", which is the normal state before the pipeline
  // has run — not an error worth showing.
  const loadDisclosure = useCallback(async () => {
    if (!appId) return;
    try {
      setProvenance(
        (await apiGet(`/los/applications/${appId}/disclosure`)) as Provenance
      );
    } catch {
      setProvenance(null);
    }
  }, [appId]);

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
    loadDisclosure();
  }, [load, loadDisclosure]);

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

  async function generateDisclosure() {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      await apiPost(`/los/applications/${appId}/disclosure`);
      await loadDisclosure();
      setActionMsg("Disclosure generated and held for compliance review.");
    } catch (err) {
      // A 422 here is the verification gate refusing the document, not an outage. The
      // reason it carries is the whole point of the gate, so it is shown verbatim.
      setActionErr(errMsg(err, "Could not generate a disclosure."));
    } finally {
      setActionBusy(false);
    }
  }

  async function transitionDisclosure(toStatus: string, reasonCode?: string) {
    if (!appId) return;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      const res = (await apiPost(
        `/los/applications/${appId}/disclosure/transition`,
        { to_status: toStatus, reason_code: reasonCode ?? null }
      )) as { status?: string; routed_to?: string | null };
      await loadDisclosure();
      setActionMsg(
        res.routed_to
          ? `Disclosure rejected; the work goes back to ${res.routed_to}.`
          : `Disclosure is now ${String(res.status || toStatus).replace(/_/g, " ")}.`
      );
    } catch (err) {
      setActionErr(errMsg(err, "Could not update the disclosure."));
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
  const disclosureStatus = provenance?.disclosure_status || null;

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
            disabled={actionBusy || !!disclosureStatus}
            title={
              disclosureStatus
                ? "A disclosure already exists for this application."
                : undefined
            }
          >
            {actionBusy ? "Working…" : "Generate disclosure"}
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
                  disabled={actionBusy || provenance.chain_complete === false}
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
