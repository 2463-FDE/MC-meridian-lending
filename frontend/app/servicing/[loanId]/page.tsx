"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useRef, useState } from "react";
import StatusChip from "../../../components/StatusChip";
import { apiGet, apiPost, getUser } from "../../../lib/api";
import { usd, pct, shortDate } from "../../../lib/format";

interface Loan {
  id: string | number;
  applicant_name: string;
  principal: number;
  apr: number;
  term_months: number;
  status: string;
  balance: number;
  past_due: number;
  opened_at: string;
}

interface ScheduleRow {
  n: number;
  due_date: string;
  payment: number;
  principal: number;
  interest: number;
  balance: number;
}

interface PaymentRow {
  id: string | number;
  amount: number;
  method: string;
  created_at: string;
  masked_pan?: string | null;
}

// Named so the initial value and the per-loan reset cannot drift apart.
const DEFAULT_PAY_AMOUNT = "250.00";

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

// 424 Failed Dependency: the charge captured but servicing refused the apply. The
// only payment failure that must not be retried -- see submitPayment.
function isCapturedUnapplied(err: unknown): boolean {
  return (
    !!err &&
    typeof err === "object" &&
    "status" in err &&
    (err as { status: unknown }).status === 424
  );
}

// 409 Conflict: adjust-balance's compare-and-set refused because the stored balance
// no longer matched what the operator was quoted (D32 second half) -- see adjustBalance.
function isBalanceConflict(err: unknown): boolean {
  return (
    !!err &&
    typeof err === "object" &&
    "status" in err &&
    (err as { status: unknown }).status === 409
  );
}

type PaymentBody = {
  loan_id: string | undefined;
  pan: string;
  amount: number;
  method: string;
};

// How the last send came back. "unresolved" is the ambiguous case -- network error,
// timeout, 5xx -- where the charge may or may not have reached the processor.
// "captured" is the 424: it definitely charged and only the ledger apply failed.
type AttemptState = "unresolved" | "resolved" | "captured";

// True when minting a fresh key would risk a second claim and a second capture --
// exactly what claim_or_branch() exists to collapse. Only a 2xx clears it.
//
// The amount deliberately does not enter this. An earlier version lifted the block
// once the form stopped describing the same charge, on the reasoning that a different
// amount is a different intent. It is not: whether the first charge captured is
// exactly what "unresolved" means is unknown, so a $250.01 charge behind a failed
// $250 one is still potentially the second capture -- and sending it overwrites the
// record, discarding the original key and the warning along with it. Editing a digit
// is not an act the borrower has to mean. The reset is, which is why it is the only
// way through (and why "captured" does not get even that -- the card charged, and the
// balance on screen does not yet reflect it).
//
// This is a speed bump, not a control. It lives in component state, so a reload or a
// route change clears it. The durable protection is server-side: the key collapses an
// exact retry, and reconciliation catches what it cannot.
function blocksNewIntent(
  last: { state: AttemptState } | null
): boolean {
  return last !== null && last.state !== "resolved";
}

export default function LoanDetailPage() {
  const params = useParams<{ loanId: string }>();
  const loanId = params?.loanId;

  const [loan, setLoan] = useState<Loan | null>(null);
  const [schedule, setSchedule] = useState<ScheduleRow[]>([]);
  const [payments, setPayments] = useState<PaymentRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [showSchedule, setShowSchedule] = useState(false);

  // action panels
  const [payAmount, setPayAmount] = useState(DEFAULT_PAY_AMOUNT);
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [newBalance, setNewBalance] = useState("");
  const [waiveAmount, setWaiveAmount] = useState("");

  // The exact request last sent to POST /payments, kept so "Retry same charge" can
  // replay it byte-for-byte (same key, same body) instead of minting a new intent.
  // The attempt state decides both affordances: whether "Retry same charge" is worth
  // offering, and whether the primary Pay button may mint a new key at all -- see
  // blocksNewIntent.
  const [lastPayment, setLastPayment] = useState<{
    key: string;
    body: PaymentBody;
    state: AttemptState;
  } | null>(null);

  // UI-only affordance: only CSR/admin SEE the money-moving rep actions
  // (adjust balance / waive fee). servicing-service enforces the same role
  // check server-side (authz.require_money_role, D8, PR #32) -- hiding the
  // buttons is not what stops a borrower from calling the route directly.
  // Still open: the gateway itself enforces no role authz on money actions
  // (D8), and one money role still makes and approves its own adjustment
  // with no second approver (maker-checker, ADR 0017, not built).
  const [canRepActions, setCanRepActions] = useState(false);
  useEffect(() => {
    const role = getUser()?.role;
    setCanRepActions(role === "csr" || role === "admin");
  }, []);

  // Route generation. Next.js reuses this page component across /servicing/[loanId]
  // navigations, so a request can outlive the loan it was fired from. Each handler captures
  // this counter when it starts and compares on completion: anything from before the last
  // navigation is dropped. Generations rather than loanIds, so navigating away and BACK to
  // the same loan still discards the earlier in-flight call.
  const routeGenRef = useRef(0);

  // Per-loan state reset. Everything below describes ONE account, so without this the
  // previous borrower's name, balance, schedule, payment history and action result stay on
  // the next account's screen, and the money-action inputs stay pre-filled with the amounts
  // typed against the previous loan (PR review sweep of the same defect found on the
  // underwriting detail page).
  //
  // This runs DURING render, not in an effect. A passive effect runs after the browser
  // paints, so the first commit for the new loanId would pair the previous borrower's name
  // and balance with the new loan number in the header before the reset ever fired (PR
  // review sweep). Adjusting state during render makes React re-run this component with the
  // cleared state and commit only that. Placed above the load effect, so the generation is
  // bumped before loadAll captures it.
  const [routeLoanId, setRouteLoanId] = useState(loanId);
  if (loanId !== routeLoanId) {
    setRouteLoanId(loanId);
    routeGenRef.current += 1;
    setLoan(null);
    setLoading(true);
    setSchedule([]);
    setPayments([]);
    setShowSchedule(false);
    setActionMsg(null);
    setActionErr(null);
    setActionBusy(false);
    setError(null);
    setPayAmount(DEFAULT_PAY_AMOUNT);
    setNewBalance("");
    setWaiveAmount("");
    setLastPayment(null);
  }

  const loadAll = useCallback(async () => {
    if (!loanId) return;
    const gen = routeGenRef.current;
    setLoading(true);
    setError(null);
    try {
      // Load loan first; schedule/payments are best-effort (tolerate failures).
      const l = (await apiGet(`/lss/loans/${loanId}`)) as Loan;
      if (routeGenRef.current !== gen) return;
      setLoan(l);
      const [sch, pay] = await Promise.allSettled([
        apiGet(`/lss/loans/${loanId}/schedule`),
        apiGet(`/lss/loans/${loanId}/payments`),
      ]);
      if (routeGenRef.current !== gen) return;
      if (sch.status === "fulfilled") {
        setSchedule((sch.value as { schedule?: ScheduleRow[] })?.schedule ?? []);
      }
      if (pay.status === "fulfilled") {
        setPayments((pay.value as { items?: PaymentRow[] })?.items ?? []);
      }
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setError(errMsg(err, "Could not load this loan."));
      setLoan(null);
    } finally {
      if (routeGenRef.current === gen) setLoading(false);
    }
  }, [loanId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Refresh only balance + payment history after an action. Takes the caller's route
  // generation so a refresh started before a navigation cannot write another loan's
  // balance onto the account now on screen. Returns whether the balance fetch itself
  // landed, so a caller relying on the refreshed figure being on screen (the 409
  // conflict message below) can tell a stale display from a current one.
  const refreshBalanceAndHistory = useCallback(async (gen: number) => {
    if (!loanId) return false;
    const [bal, pay] = await Promise.allSettled([
      apiGet(`/lss/accounts/${loanId}/balance`),
      apiGet(`/lss/loans/${loanId}/payments`),
    ]);
    if (routeGenRef.current !== gen) return false;
    if (bal.status === "fulfilled") {
      const b = bal.value as { balance?: number; past_due?: number };
      setLoan((prev) =>
        prev
          ? {
              ...prev,
              balance: b.balance ?? prev.balance,
              past_due: b.past_due ?? prev.past_due,
            }
          : prev
      );
    }
    if (pay.status === "fulfilled") {
      setPayments((pay.value as { items?: PaymentRow[] })?.items ?? []);
    }
    return bal.status === "fulfilled";
  }, [loanId]);

  // Shared by makePayment (fresh key, new intent) and retryPayment (same key, same
  // body) so both send the request the exact same way — POST /payments requires the
  // header (ADR 0013 Decision 1) and collapses a retry under one key server-side.
  async function submitPayment(
    gen: number,
    key: string,
    body: PaymentBody,
    successMsg: string
  ) {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    // Record the intent BEFORE the send. A network error, timeout or 5xx leaves the
    // outcome unknown -- the charge may have reached the processor -- so the key has
    // to survive the failure for the retry affordance to render. Storing it after the
    // await would lose the key on exactly the ambiguous case, and the next Pay click
    // would mint a fresh one and capture a second time.
    setLastPayment({ key, body, state: "unresolved" });
    try {
      await apiPost("/payments", body, { "Idempotency-Key": key });
      if (routeGenRef.current !== gen) return;
      // A 2xx resolves the charge, so the gate lifts: a borrower paying the same
      // amount again on purpose gets a new key rather than being blocked.
      setLastPayment({ key, body, state: "resolved" });
      setActionMsg(successMsg);
      await refreshBalanceAndHistory(gen);
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      // 424 Failed Dependency is the one non-retryable outcome: the charge captured
      // and only the apply failed, so replaying the key returns that same captured
      // payment and the balance stays uncredited. Drop the retry affordance rather
      // than offer it next to a message telling the borrower not to retry -- but keep
      // the attempt on record, because a card that definitely charged is the LAST
      // state in which Pay should be free to mint another key. Every other failure --
      // network error, timeout, 5xx, a rejected claim -- stays retryable under the
      // original key.
      if (isCapturedUnapplied(err)) {
        setLastPayment({ key, body, state: "captured" });
      }
      setActionErr(errMsg(err, "Payment failed."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function makePayment() {
    // The disabled attribute on the button is cosmetic on its own; the refusal lives
    // here so a click that lands before the re-render cannot mint a key either.
    if (blocksNewIntent(lastPayment)) return;
    const gen = routeGenRef.current;
    const key = crypto.randomUUID();
    const body: PaymentBody = {
      loan_id: loanId,
      pan: "4111111111111111", // hardcoded test card PAN (texture)
      amount: parseFloat(payAmount || "0"),
      method: "card",
    };
    await submitPayment(gen, key, body, `Payment of ${usd(payAmount)} submitted.`);
  }

  async function retryPayment() {
    // The button is hidden for a captured attempt, but the refusal belongs here too:
    // "no caller renders it" is the assumption that let the reset escape reach this
    // state in the first place.
    if (!lastPayment || lastPayment.state === "captured") return;
    const gen = routeGenRef.current;
    await submitPayment(
      gen,
      lastPayment.key,
      lastPayment.body,
      "Retry submitted with the same Idempotency-Key — collapsed to the original payment."
    );
  }

  async function adjustBalance() {
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // servicing-service rejects a non-money role with a 403 (authz.require_money_role, D8)
      await apiPost(`/lss/accounts/${loanId}/adjust-balance`, {
        new_balance: parseFloat(newBalance || "0"),
        // The balance last shown on screen -- servicing refuses (409) if the stored
        // value has moved since, rather than silently overwriting whatever changed
        // it (D32 second half: a compare-and-set, not a blind SET).
        expected_balance: loan?.balance,
      });
      if (routeGenRef.current !== gen) return;
      setActionMsg(`Balance adjusted to ${usd(newBalance)}.`);
      await refreshBalanceAndHistory(gen);
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      if (isBalanceConflict(err)) {
        // Refuse to guess what the operator meant against a figure that has moved.
        // Refresh so the KPI/placeholder show the real balance, and require them to
        // review it and resubmit deliberately -- never auto-retry a money write.
        const refreshed = await refreshBalanceAndHistory(gen);
        if (routeGenRef.current !== gen) return;
        if (refreshed) {
          setActionErr(
            "Balance changed since it was quoted. Review the current balance above and resubmit."
          );
        } else {
          // The screen still shows the stale quoted figure -- telling the operator
          // to "review the current balance above" here would point at the exact
          // number the compare-and-set just refused. Fall back to the 409's own
          // detail (it names the current balance) so a deliberate resubmit is
          // never made against a number this screen never actually confirmed.
          setActionErr(
            errMsg(
              err,
              "Balance changed since it was quoted, and the current balance could not be refreshed. Reload before resubmitting."
            )
          );
        }
      } else {
        setActionErr(errMsg(err, "Balance adjustment failed."));
      }
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  async function waiveFee() {
    const gen = routeGenRef.current;
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // servicing-service rejects a non-money role with a 403 (authz.require_money_role, D8)
      await apiPost(`/lss/accounts/${loanId}/waive-fee`, {
        amount: parseFloat(waiveAmount || "0"),
      });
      if (routeGenRef.current !== gen) return;
      setActionMsg(`Fee of ${usd(waiveAmount)} waived.`);
      await refreshBalanceAndHistory(gen);
    } catch (err) {
      if (routeGenRef.current !== gen) return;
      setActionErr(errMsg(err, "Fee waiver failed."));
    } finally {
      if (routeGenRef.current === gen) setActionBusy(false);
    }
  }

  if (loading && !loan) {
    return (
      <main className="wrap">
        <p className="muted">Loading loan #{loanId}…</p>
      </main>
    );
  }

  if (error && !loan) {
    return (
      <main className="wrap">
        <p>
          <Link href="/servicing">← Back to servicing</Link>
        </p>
        <div className="alert alert-error">{error}</div>
      </main>
    );
  }

  // Blocks the primary Pay button while the last attempt is not known to have
  // finished. Only a 2xx or the explicit reset lifts it.
  const payBlocked = blocksNewIntent(lastPayment);
  // The ambiguity warning and the reset describe a SETTLED attempt. actionBusy is
  // still true mid-flight, when nothing has failed yet.
  const attemptSettled = lastPayment !== null && !actionBusy;

  return (
    <main className="wrap">
      <p style={{ marginBottom: 12 }}>
        <Link href="/servicing">← Back to servicing</Link>
      </p>

      {/* Header */}
      <div className="spread">
        <div>
          <h1 style={{ marginBottom: 6 }}>
            {loan?.applicant_name || "Loan account"}
          </h1>
          <p className="sub" style={{ margin: 0 }}>
            Loan #{String(loanId)}
          </p>
        </div>
        {loan ? <StatusChip status={loan.status} /> : null}
      </div>

      {/* Balance / terms cards */}
      <div className="grid grid-3" style={{ margin: "20px 0" }}>
        <div className="kpi">
          <div className="kpi-label">Current balance</div>
          <div className="kpi-value">{usd(loan?.balance)}</div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Past due</div>
          <div className={`kpi-value${(loan?.past_due ?? 0) > 0 ? " danger" : ""}`}>
            {usd(loan?.past_due)}
          </div>
        </div>
        <div className="kpi">
          <div className="kpi-label">Opened</div>
          <div className="kpi-value" style={{ fontSize: 20 }}>
            {shortDate(loan?.opened_at)}
          </div>
        </div>
      </div>

      <div className="card">
        <div className="card-title" style={{ marginBottom: 8 }}>
          Loan terms
        </div>
        <div className="dl">
          <div className="dl-row">
            <dt>Original principal</dt>
            <dd>{usd(loan?.principal)}</dd>
          </div>
          <div className="dl-row">
            <dt>APR</dt>
            <dd>{pct(loan?.apr)}</dd>
          </div>
          <div className="dl-row">
            <dt>Term</dt>
            <dd>{loan?.term_months} months</dd>
          </div>
          <div className="dl-row">
            <dt>Status</dt>
            <dd>{loan ? <StatusChip status={loan.status} /> : "—"}</dd>
          </div>
        </div>
      </div>

      {/* Amortization schedule */}
      <h2>Amortization schedule</h2>
      {schedule.length === 0 ? (
        <div className="card">
          <p className="muted" style={{ margin: 0 }}>
            No schedule available for this loan.
          </p>
        </div>
      ) : (
        <>
          <button
            className="collapse-toggle"
            onClick={() => setShowSchedule((v) => !v)}
          >
            {showSchedule ? "Hide" : "Show"} schedule ({schedule.length} payments)
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
                    <th className="num">Remaining balance</th>
                  </tr>
                </thead>
                <tbody>
                  {schedule.map((r) => (
                    <tr key={r.n}>
                      <td>{r.n}</td>
                      <td>{shortDate(r.due_date)}</td>
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
        </>
      )}

      {/* Payment history */}
      <h2>Payment history</h2>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Date</th>
              <th>Method</th>
              <th>Card</th>
              <th className="num">Amount</th>
            </tr>
          </thead>
          <tbody>
            {payments.length === 0 ? (
              <tr>
                <td colSpan={4} className="empty">
                  No payments recorded yet.
                </td>
              </tr>
            ) : (
              payments.map((p) => (
                <tr key={String(p.id)}>
                  <td>{shortDate(p.created_at)}</td>
                  <td style={{ textTransform: "capitalize" }}>{p.method}</td>
                  <td>{p.masked_pan || "ACH"}</td>
                  <td className="num">{usd(p.amount)}</td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      {/* Action feedback (shared by all panels) */}
      {actionMsg ? <div className="alert alert-success">{actionMsg}</div> : null}
      {actionErr ? <div className="alert alert-error">{actionErr}</div> : null}

      {/* Make a payment */}
      <h2>Make a payment</h2>
      <div className="card">
        <div className="row" style={{ alignItems: "flex-end" }}>
          <div className="field">
            <label>Amount (USD)</label>
            <input
              type="number"
              min="0"
              step="0.01"
              value={payAmount}
              onChange={(e) => setPayAmount(e.target.value)}
            />
          </div>
          <button onClick={makePayment} disabled={actionBusy || payBlocked}>
            {actionBusy ? "Processing…" : "Pay with card on file"}
          </button>
        </div>
        <p className="hint" style={{ marginTop: 10 }}>
          Charged to card ending 1111. Payments post immediately.
        </p>
        {lastPayment ? (
          <p className="hint" style={{ marginTop: 10 }}>
            Idempotency-Key: <code>{lastPayment.key}</code>{" "}
            {lastPayment.state !== "captured" ? (
              <button
                onClick={retryPayment}
                disabled={actionBusy}
                style={{ marginLeft: 8 }}
              >
                Retry same charge
              </button>
            ) : null}
            {attemptSettled && lastPayment.state === "unresolved" ? (
              <>
                <button
                  onClick={() => setLastPayment(null)}
                  disabled={actionBusy}
                  style={{ marginLeft: 8 }}
                >
                  Start a new payment
                </button>
                <br />
                This charge may already have gone through. Retry it under the same
                key, or start a new payment to charge the card again.
              </>
            ) : null}
          </p>
        ) : null}
      </div>

      {/* Rep actions — UI-only affordance, shown only to CSR/admin. */}
      {/* servicing-service enforces the same role check server-side (D8, PR #32); */}
      {/* the gateway itself still enforces no role authz on money actions (D8 open half). */}
      {canRepActions ? (
        <>
          <h2>Servicing rep actions</h2>
          <div className="grid grid-2">
            <div className="card">
              <div className="card-title" style={{ marginBottom: 10 }}>
                Adjust balance
              </div>
              <label>New balance (USD)</label>
              <input
                type="number"
                step="0.01"
                value={newBalance}
                onChange={(e) => setNewBalance(e.target.value)}
                placeholder={loan ? String(loan.balance) : "0.00"}
              />
              <button
                className="btn-ghost btn-block"
                style={{ marginTop: 14 }}
                onClick={adjustBalance}
                disabled={actionBusy || !newBalance}
              >
                Adjust balance
              </button>
            </div>
            <div className="card">
              <div className="card-title" style={{ marginBottom: 10 }}>
                Waive fee
              </div>
              <label>Waiver amount (USD)</label>
              <input
                type="number"
                step="0.01"
                value={waiveAmount}
                onChange={(e) => setWaiveAmount(e.target.value)}
                placeholder="0.00"
              />
              <button
                className="btn-ghost btn-block"
                style={{ marginTop: 14 }}
                onClick={waiveFee}
                disabled={actionBusy || !waiveAmount}
              >
                Waive fee
              </button>
            </div>
          </div>
        </>
      ) : null}
    </main>
  );
}
