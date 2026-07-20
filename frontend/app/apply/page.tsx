"use client";

import Link from "next/link";
import { useEffect, useState } from "react";
import Stepper, { type Step } from "../../components/Stepper";
import StatusChip from "../../components/StatusChip";
import { apiGet, apiPost } from "../../lib/api";
import { usd, pct } from "../../lib/format";

// Resume state (ADR 0010 Phase B, PR #7 review). The continuation token is NO LONGER held
// by the browser: the gateway keeps it server-side and hands back an HttpOnly resume cookie
// that JS cannot read (so XSS / a compromised script cannot exfiltrate the credential). All
// we persist here is the (non-sensitive) app id, so a refresh/tab-close can re-open the
// application; the cookie -- sent automatically with credentials:"include" -- is what
// actually authorizes the resumed request.
const RESUME_KEY = "meridian:apply:resume";

function saveResume(appId: string | number) {
  if (typeof window === "undefined" || appId == null) return;
  try {
    window.localStorage.setItem(RESUME_KEY, JSON.stringify({ app_id: appId }));
  } catch {
    // storage unavailable (private mode / quota) -- in-memory state still works this session
  }
}

function readResume(): { app_id: string | number } | null {
  if (typeof window === "undefined") return null;
  try {
    const raw = window.localStorage.getItem(RESUME_KEY);
    if (!raw) return null;
    const v = JSON.parse(raw);
    return v && v.app_id != null ? v : null;
  } catch {
    return null;
  }
}

function clearResume() {
  if (typeof window === "undefined") return;
  try {
    window.localStorage.removeItem(RESUME_KEY);
  } catch {
    // ignore
  }
}

const STEPS: Step[] = [
  { n: 1, label: "Personal" },
  { n: 2, label: "Employment & Income" },
  { n: 3, label: "Loan Details" },
  { n: 4, label: "Review" },
  { n: 5, label: "Decision & Offer" },
];

const PURPOSES = [
  { value: "debt_consolidation", label: "Debt consolidation" },
  { value: "home_improvement", label: "Home improvement" },
  { value: "auto", label: "Auto" },
  { value: "medical", label: "Medical" },
  { value: "personal", label: "Personal" },
  { value: "other", label: "Other" },
];

const OFFER_RATE_PCT = 7.99;

interface FormState {
  name: string;
  dob: string;
  ssn: string;
  email: string;
  phone: string;
  address: string;
  employer: string;
  job_title: string;
  annual_income: string;
  monthly_debt: string;
  employment_years: string;
  amount: number;
  term_months: string;
  purpose: string;
}

interface Kyc {
  name_verified?: boolean;
  dob_verified?: boolean;
  address_verified?: boolean;
  ssn_verified?: boolean;
}

interface AppResult {
  app_id: string | number;
  status?: string;
  kyc?: Kyc;
  // False when the KYC service call did not complete at submit (outage/timeout/auth) --
  // distinct from a KYC that ran and declined. Under the mandatory KYC gate (ADR 0011)
  // such an application 409s on decision/offer/accept until a recheck persists a passing
  // row, so the UI must offer a retry rather than the (misleading) all-false KYC result.
  kyc_checked?: boolean;
  // ADR 0010 (PR #7 review): the continuation token is no longer returned to the browser.
  // The gateway strips it from this response, stores it server-side, and authorizes the
  // logged-out applicant via an HttpOnly resume cookie the client cannot read.
}

interface DecisionResult {
  app_id: string | number;
  decision: string;
  score?: number;
  adverse_action_reason?: string;
}

interface Disclosure {
  apr: number;
  finance_charge: number;
  monthly_payment: number;
  amount_financed: number;
  total_of_payments: number;
  schedule?: {
    n: number;
    due_date: string;
    payment: number;
    principal: number;
    interest: number;
    balance: number;
  }[];
}

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

export default function ApplyPage() {
  const [step, setStep] = useState(1);
  const [form, setForm] = useState<FormState>({
    name: "",
    dob: "",
    ssn: "",
    email: "",
    phone: "",
    address: "",
    employer: "",
    job_title: "",
    annual_income: "",
    monthly_debt: "",
    employment_years: "",
    amount: 15000,
    term_months: "36",
    purpose: "debt_consolidation",
  });
  const [errors, setErrors] = useState<Record<string, string>>({});

  // submission / decision / offer state
  const [busy, setBusy] = useState(false);
  const [apiError, setApiError] = useState<string | null>(null);
  const [app, setApp] = useState<AppResult | null>(null);
  const [decision, setDecision] = useState<DecisionResult | null>(null);
  const [disclosure, setDisclosure] = useState<Disclosure | null>(null);
  const [acceptedLoanId, setAcceptedLoanId] = useState<string | number | null>(
    null
  );
  const [showSchedule, setShowSchedule] = useState(false);
  const [resuming, setResuming] = useState(false);

  // Resume a submitted application after a refresh / tab close: the continuation token was
  // persisted at submit, so rehydrate it and re-fetch the application (the token authorizes
  // the read) to restore the step-5 view. A stale/invalid token or a deleted app clears the
  // saved state and starts fresh.
  useEffect(() => {
    const saved = readResume();
    if (!saved) return;
    let cancelled = false;
    setResuming(true);
    (async () => {
      try {
        // No token header: the resume cookie (sent via credentials:"include") is what
        // authorizes this read; the gateway re-attaches the token server-side.
        const d = (await apiGet(`/los/applications/${saved.app_id}`)) as {
          status?: string;
          kyc?: Kyc | null;
          decision?: string | null;
          offer?: Disclosure | null;
        };
        if (cancelled) return;
        setApp({
          app_id: saved.app_id,
          status: d.status,
          kyc: d.kyc ?? undefined,
          // No persisted KYC row -> the check never completed (outage) -> offer the retry.
          kyc_checked: !!d.kyc,
        });
        if (d.decision) setDecision({ app_id: saved.app_id, decision: d.decision });
        if (d.offer) setDisclosure(d.offer);
        setStep(5);
      } catch {
        clearResume();
      } finally {
        if (!cancelled) setResuming(false);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  function set<K extends keyof FormState>(k: K, v: FormState[K]) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  function validateStep(s: number): boolean {
    const e: Record<string, string> = {};
    if (s === 1) {
      if (!form.name.trim()) e.name = "Required";
      if (!form.dob) e.dob = "Required";
      if (!form.ssn.trim()) e.ssn = "Required";
      else if (!/^\d{3}-?\d{2}-?\d{4}$/.test(form.ssn.trim()))
        e.ssn = "Enter a valid SSN (###-##-####)";
      if (!form.email.trim()) e.email = "Required";
      else if (!/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(form.email))
        e.email = "Enter a valid email";
      if (!form.phone.trim()) e.phone = "Required";
      else if (form.phone.replace(/\D/g, "").length !== 10)
        e.phone = "Enter a valid 10-digit phone number";
      if (!form.address.trim()) e.address = "Required";
    } else if (s === 2) {
      if (!form.employer.trim()) e.employer = "Required";
      if (!form.job_title.trim()) e.job_title = "Required";
      if (!form.annual_income.trim()) e.annual_income = "Required";
      else if (Number(form.annual_income) <= 0)
        e.annual_income = "Must be greater than 0";
      if (!form.monthly_debt.trim()) e.monthly_debt = "Required";
      else if (Number(form.monthly_debt) < 0)
        e.monthly_debt = "Cannot be negative";
      if (!form.employment_years.trim()) e.employment_years = "Required";
      else if (Number(form.employment_years) < 0)
        e.employment_years = "Cannot be negative";
    }
    setErrors(e);
    return Object.keys(e).length === 0;
  }

  function next() {
    if (validateStep(step)) {
      setErrors({});
      setStep((s) => Math.min(5, s + 1));
    }
  }
  function back() {
    setErrors({});
    setStep((s) => Math.max(1, s - 1));
  }

  async function submitApplication() {
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost("/los/applications", {
        name: form.name,
        dob: form.dob,
        ssn: form.ssn,
        address: form.address,
        email: form.email,
        phone: form.phone,
        employer: form.employer,
        job_title: form.job_title,
        income: parseFloat(form.annual_income || "0"),
        monthly_debt: parseFloat(form.monthly_debt || "0"),
        employment_years: parseInt(form.employment_years || "0", 10),
        amount: form.amount,
        term_months: parseInt(form.term_months, 10),
        purpose: form.purpose,
      })) as AppResult;
      setApp(res);
      saveResume(res.app_id);
      setStep(5);
    } catch (err) {
      setApiError(errMsg(err, "Could not submit your application."));
    } finally {
      setBusy(false);
    }
  }

  // ADR 0010 (PR #7 review): the logged-out applicant's decision/offer/accept are
  // authorized by the HttpOnly resume cookie (sent automatically), not a client header.
  // Officers/owners are authorized by their session. No token is handled in the browser.

  // KYC recovery (ADR 0011): submit stays resilient during a KYC-service outage
  // (kyc_checked=false, no persisted row), so the mandatory gate 409s decision/offer/
  // accept. Re-run KYC for THIS application instead of resubmitting (which would create a
  // duplicate); the resume cookie authorizes it. setApp carries the fresh kyc + kyc_checked.
  async function recheckKyc() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost(
        `/los/applications/${app.app_id}/recheck-kyc`
      )) as AppResult;
      setApp(res);
      saveResume(res.app_id);
    } catch (err) {
      setApiError(errMsg(err, "Could not re-run identity verification."));
    } finally {
      setBusy(false);
    }
  }

  async function getDecision() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost(
        `/los/applications/${app.app_id}/decision`,
        undefined,
        {
          // Stable per-application idempotency key: a timeout retry or a second click
          // replays the recorded decision instead of re-pulling credit and appending
          // another regulated decision event (PR review).
          "Idempotency-Key": `los-decision-${app.app_id}`,
        }
      )) as DecisionResult;
      setDecision(res);
    } catch (err) {
      setApiError(errMsg(err, "Could not retrieve a decision."));
    } finally {
      setBusy(false);
    }
  }

  async function viewOffer() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost("/los/offer", {
        app_id: app.app_id,
        principal: form.amount,
        annual_rate_pct: OFFER_RATE_PCT,
        term_months: parseInt(form.term_months, 10),
      })) as { app_id: string | number; disclosure: Disclosure };
      setDisclosure(res.disclosure);
    } catch (err) {
      setApiError(errMsg(err, "Could not generate your offer."));
    } finally {
      setBusy(false);
    }
  }

  async function acceptOffer() {
    if (!app) return;
    setBusy(true);
    setApiError(null);
    try {
      const res = (await apiPost(
        `/los/applications/${app.app_id}/accept`
      )) as { loan_id: string | number };
      setAcceptedLoanId(res.loan_id);
      // Flow complete: drop the saved app id. The gateway also revokes the server-side
      // resume session and clears the HttpOnly cookie on this accept response.
      clearResume();
    } catch (err) {
      setApiError(errMsg(err, "Could not accept the offer."));
    } finally {
      setBusy(false);
    }
  }

  // decision-service emits the outcome enum "approve" (not "approved"): match it, or
  // the "View your offer" CTA never renders for an approved borrower (PR review).
  const decisionApproved = (decision?.decision || "").toLowerCase() === "approve";

  return (
    <main className="wrap">
      <h1>Apply for a personal loan</h1>
      <p className="sub">
        Fixed-rate installment loan · $1,000–$50,000 · 12–60 months
      </p>

      {resuming ? (
        <div className="alert alert-info">Resuming your saved application…</div>
      ) : null}

      <Stepper steps={STEPS} current={step} />

      <div className="card">
        {/* ---- Step 1: Personal --------------------------------------- */}
        {step === 1 && (
          <>
            <div className="card-title" style={{ marginBottom: 4 }}>
              Step 1 · Personal information
            </div>
            <Field label="Full name" error={errors.name}>
              <input
                value={form.name}
                onChange={(e) => set("name", e.target.value)}
                placeholder="Jane Q. Borrower"
              />
            </Field>
            <div className="field-row">
              <Field label="Date of birth" error={errors.dob}>
                <input
                  type="date"
                  value={form.dob}
                  onChange={(e) => set("dob", e.target.value)}
                />
              </Field>
              <Field label="Social Security Number" error={errors.ssn}>
                <input
                  value={form.ssn}
                  onChange={(e) => set("ssn", e.target.value)}
                  placeholder="###-##-####"
                  inputMode="numeric"
                  maxLength={11}
                />
              </Field>
            </div>
            <div className="field-row">
              <Field label="Email" error={errors.email}>
                <input
                  type="email"
                  value={form.email}
                  onChange={(e) => set("email", e.target.value)}
                  placeholder="you@example.com"
                />
              </Field>
              <Field label="Phone" error={errors.phone}>
                <input
                  value={form.phone}
                  onChange={(e) => set("phone", e.target.value)}
                  placeholder="(555) 555-0123"
                  inputMode="tel"
                  maxLength={14}
                />
              </Field>
            </div>
            <Field label="Home address" error={errors.address}>
              <input
                value={form.address}
                onChange={(e) => set("address", e.target.value)}
                placeholder="123 Main St, Springfield, IL 62704"
              />
            </Field>
          </>
        )}

        {/* ---- Step 2: Employment & Income ---------------------------- */}
        {step === 2 && (
          <>
            <div className="card-title" style={{ marginBottom: 4 }}>
              Step 2 · Employment & income
            </div>
            <div className="field-row">
              <Field label="Employer" error={errors.employer}>
                <input
                  value={form.employer}
                  onChange={(e) => set("employer", e.target.value)}
                />
              </Field>
              <Field label="Job title" error={errors.job_title}>
                <input
                  value={form.job_title}
                  onChange={(e) => set("job_title", e.target.value)}
                />
              </Field>
            </div>
            <div className="field-row">
              <Field label="Annual income (USD)" error={errors.annual_income}>
                <input
                  type="number"
                  min="0"
                  value={form.annual_income}
                  onChange={(e) => set("annual_income", e.target.value)}
                  placeholder="65000"
                />
              </Field>
              <Field
                label="Years at employer"
                error={errors.employment_years}
              >
                <input
                  type="number"
                  min="0"
                  step="0.5"
                  value={form.employment_years}
                  onChange={(e) => set("employment_years", e.target.value)}
                  placeholder="3"
                />
              </Field>
            </div>
            <div className="field-row">
              <Field
                label="Monthly debt payments (USD)"
                error={errors.monthly_debt}
              >
                <input
                  type="number"
                  min="0"
                  value={form.monthly_debt}
                  onChange={(e) => set("monthly_debt", e.target.value)}
                  placeholder="500"
                />
              </Field>
            </div>
          </>
        )}

        {/* ---- Step 3: Loan Details ----------------------------------- */}
        {step === 3 && (
          <>
            <div className="card-title" style={{ marginBottom: 4 }}>
              Step 3 · Loan details
            </div>
            <label htmlFor="amount">Loan amount</label>
            <div className="range-readout">{usd(form.amount)}</div>
            <input
              id="amount"
              type="range"
              min={1000}
              max={50000}
              step={500}
              value={form.amount}
              onChange={(e) => set("amount", Number(e.target.value))}
            />
            <div className="spread">
              <span className="hint">{usd(1000)}</span>
              <span className="hint">{usd(50000)}</span>
            </div>

            <div className="field-row" style={{ marginTop: 8 }}>
              <Field label="Term (months)">
                <select
                  value={form.term_months}
                  onChange={(e) => set("term_months", e.target.value)}
                >
                  {["12", "24", "36", "48", "60"].map((t) => (
                    <option key={t} value={t}>
                      {t} months
                    </option>
                  ))}
                </select>
              </Field>
              <Field label="Purpose">
                <select
                  value={form.purpose}
                  onChange={(e) => set("purpose", e.target.value)}
                >
                  {PURPOSES.map((p) => (
                    <option key={p.value} value={p.value}>
                      {p.label}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
            <p className="hint" style={{ marginTop: 12 }}>
              Estimated rate {pct(OFFER_RATE_PCT)} APR (illustrative — your final
              rate is set at offer).
            </p>
          </>
        )}

        {/* ---- Step 4: Review ----------------------------------------- */}
        {step === 4 && (
          <>
            <div className="card-title" style={{ marginBottom: 12 }}>
              Step 4 · Review your application
            </div>
            <SummaryGroup title="Personal">
              <SummaryRow label="Full name" value={form.name} />
              <SummaryRow label="Date of birth" value={form.dob} />
              <SummaryRow label="SSN" value={form.ssn} />
              <SummaryRow label="Email" value={form.email} />
              <SummaryRow label="Phone" value={form.phone} />
              <SummaryRow label="Address" value={form.address} />
            </SummaryGroup>
            <SummaryGroup title="Employment & income">
              <SummaryRow label="Employer" value={form.employer} />
              <SummaryRow label="Job title" value={form.job_title} />
              <SummaryRow
                label="Annual income"
                value={usd(form.annual_income)}
              />
              <SummaryRow
                label="Monthly debt payments"
                value={usd(form.monthly_debt)}
              />
              <SummaryRow
                label="Years at employer"
                value={form.employment_years}
              />
            </SummaryGroup>
            <SummaryGroup title="Loan details">
              <SummaryRow label="Amount" value={usd(form.amount)} />
              <SummaryRow
                label="Term"
                value={`${form.term_months} months`}
              />
              <SummaryRow
                label="Purpose"
                value={
                  PURPOSES.find((p) => p.value === form.purpose)?.label ||
                  form.purpose
                }
              />
            </SummaryGroup>

            {apiError ? (
              <div className="alert alert-error">{apiError}</div>
            ) : null}

            <button
              className="btn-block"
              onClick={submitApplication}
              disabled={busy}
            >
              {busy ? "Submitting…" : "Submit application"}
            </button>
          </>
        )}

        {/* ---- Step 5: Decision & Offer ------------------------------- */}
        {step === 5 && (
          <>
            <div className="card-title" style={{ marginBottom: 12 }}>
              Step 5 · Decision & offer
            </div>

            {!app ? (
              <div className="alert alert-warn">
                Submit your application in the previous step to continue.
              </div>
            ) : (
              <>
                <div className="alert alert-info">
                  Application <strong>#{String(app.app_id)}</strong> received.
                </div>

                {app.kyc_checked === false ? (
                  <>
                    <h3 style={{ marginTop: 18 }}>Identity verification (KYC)</h3>
                    <div className="alert alert-warn">
                      We couldn&apos;t complete identity verification just now —
                      this is usually a temporary issue, not a decline. Retry to
                      continue; you won&apos;t need to re-enter anything.
                    </div>
                    <button onClick={recheckKyc} disabled={busy}>
                      {busy ? "Re-checking…" : "Retry identity check"}
                    </button>
                  </>
                ) : (
                  <>
                    {app.kyc ? (
                      <>
                        <h3 style={{ marginTop: 18 }}>
                          Identity verification (KYC)
                        </h3>
                        <div className="dl">
                          <KycRow label="Name" ok={app.kyc.name_verified} />
                          <KycRow
                            label="Date of birth"
                            ok={app.kyc.dob_verified}
                          />
                          <KycRow
                            label="Address"
                            ok={app.kyc.address_verified}
                          />
                          <KycRow label="SSN" ok={app.kyc.ssn_verified} />
                        </div>
                      </>
                    ) : null}

                    <hr className="divider" />

                    {!decision ? (
                      <button onClick={getDecision} disabled={busy}>
                        {busy ? "Evaluating…" : "Get decision"}
                      </button>
                    ) : (
                  <>
                    <div className="spread">
                      <h3 style={{ margin: 0 }}>Underwriting decision</h3>
                      <StatusChip status={decision.decision} />
                    </div>
                    {typeof decision.score === "number" ? (
                      <p className="hint">Model score: {decision.score}</p>
                    ) : null}
                    {decision.adverse_action_reason ? (
                      <div className="alert alert-warn">
                        <strong>Adverse action reason:</strong>{" "}
                        {decision.adverse_action_reason}
                      </div>
                    ) : null}

                    {decisionApproved && !disclosure ? (
                      <button
                        style={{ marginTop: 16 }}
                        onClick={viewOffer}
                        disabled={busy}
                      >
                        {busy ? "Preparing offer…" : "View your offer"}
                      </button>
                    ) : null}
                      </>
                    )}
                  </>
                )}

                {disclosure ? (
                  <OfferPanel
                    disclosure={disclosure}
                    amount={form.amount}
                    termMonths={form.term_months}
                    showSchedule={showSchedule}
                    onToggleSchedule={() => setShowSchedule((v) => !v)}
                    onAccept={acceptOffer}
                    busy={busy}
                    acceptedLoanId={acceptedLoanId}
                  />
                ) : null}

                {apiError ? (
                  <div className="alert alert-error">{apiError}</div>
                ) : null}
              </>
            )}
          </>
        )}

        {/* ---- Wizard nav (steps 1-4) -------------------------------- */}
        {step < 4 && (
          <div className="btn-row between">
            <button
              className="btn-ghost"
              onClick={back}
              disabled={step === 1}
            >
              Back
            </button>
            <button onClick={next}>Next</button>
          </div>
        )}
        {step === 4 && (
          <div className="btn-row">
            <button className="btn-ghost" onClick={back} disabled={busy}>
              Back
            </button>
          </div>
        )}
      </div>
    </main>
  );
}

// ---- small presentational helpers ---------------------------------------

function Field({
  label,
  error,
  children,
}: {
  label: string;
  error?: string;
  children: React.ReactNode;
}) {
  return (
    <div>
      <label>{label}</label>
      {children}
      {error ? <div className="field-error">{error}</div> : null}
    </div>
  );
}

function SummaryGroup({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div style={{ marginBottom: 18 }}>
      <div className="card-title" style={{ marginBottom: 6 }}>
        {title}
      </div>
      <div className="dl">{children}</div>
    </div>
  );
}

function SummaryRow({ label, value }: { label: string; value: string }) {
  return (
    <div className="dl-row">
      <dt>{label}</dt>
      <dd>{value || "—"}</dd>
    </div>
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

function OfferPanel({
  disclosure,
  amount,
  termMonths,
  showSchedule,
  onToggleSchedule,
  onAccept,
  busy,
  acceptedLoanId,
}: {
  disclosure: Disclosure;
  amount: number;
  termMonths: string;
  showSchedule: boolean;
  onToggleSchedule: () => void;
  onAccept: () => void;
  busy: boolean;
  acceptedLoanId: string | number | null;
}) {
  const hasSchedule = !!disclosure.schedule && disclosure.schedule.length > 0;
  return (
    <div style={{ marginTop: 22 }}>
      <h3>Your offer</h3>
      <p className="hint" style={{ marginBottom: 12 }}>
        {usd(amount)} over {termMonths} months · monthly payment{" "}
        <strong>{usd(disclosure.monthly_payment)}</strong>
      </p>

      {/* Classic 4-box Federal Truth-in-Lending disclosure layout. */}
      <div className="tila">
        <div className="tila-title">Federal Truth-in-Lending Disclosure</div>
        <div className="tila-grid">
          <div className="tila-cell tila-cell-apr">
            <div className="tila-cell-label">Annual Percentage Rate</div>
            <div className="tila-cell-desc">
              The cost of your credit as a yearly rate.
            </div>
            <div className="tila-cell-value">{pct(disclosure.apr)}</div>
          </div>
          <div className="tila-cell">
            <div className="tila-cell-label">Finance Charge</div>
            <div className="tila-cell-desc">
              The dollar amount the credit will cost you.
            </div>
            <div className="tila-cell-value">
              {usd(disclosure.finance_charge)}
            </div>
          </div>
          <div className="tila-cell">
            <div className="tila-cell-label">Amount Financed</div>
            <div className="tila-cell-desc">
              The amount of credit provided to you.
            </div>
            <div className="tila-cell-value">
              {usd(disclosure.amount_financed)}
            </div>
          </div>
          <div className="tila-cell">
            <div className="tila-cell-label">Total of Payments</div>
            <div className="tila-cell-desc">
              What you will have paid after all payments are made.
            </div>
            <div className="tila-cell-value">
              {usd(disclosure.total_of_payments)}
            </div>
          </div>
        </div>
      </div>

      {hasSchedule ? (
        <div style={{ marginTop: 16 }}>
          <button className="collapse-toggle" onClick={onToggleSchedule}>
            {showSchedule ? "Hide" : "Show"} payment schedule (
            {disclosure.schedule!.length})
          </button>
          {showSchedule ? (
            <div className="table-wrap table-scroll" style={{ marginTop: 12 }}>
              <table>
                <thead>
                  <tr>
                    <th>#</th>
                    <th>Due date</th>
                    <th className="num">Payment</th>
                    <th className="num">Principal</th>
                    <th className="num">Interest</th>
                    <th className="num">Balance</th>
                  </tr>
                </thead>
                <tbody>
                  {disclosure.schedule!.map((r) => (
                    <tr key={r.n}>
                      <td>{r.n}</td>
                      <td>{r.due_date}</td>
                      <td className="num">{usd(r.payment)}</td>
                      <td className="num">{usd(r.principal)}</td>
                      <td className="num">{usd(r.interest)}</td>
                      <td className="num">{usd(r.balance)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          ) : null}
        </div>
      ) : null}

      {acceptedLoanId ? (
        <div className="alert alert-success">
          Offer accepted. Loan <strong>#{String(acceptedLoanId)}</strong>{" "}
          created.{" "}
          <Link href={`/servicing/${acceptedLoanId}`}>
            Go to your loan account →
          </Link>
        </div>
      ) : (
        <button style={{ marginTop: 16 }} onClick={onAccept} disabled={busy}>
          {busy ? "Accepting…" : "Accept offer"}
        </button>
      )}
    </div>
  );
}
