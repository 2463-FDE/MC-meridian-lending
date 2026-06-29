"use client";

import Link from "next/link";
import { useParams } from "next/navigation";
import { useCallback, useEffect, useState } from "react";
import StatusChip from "../../../components/StatusChip";
import { apiGet, apiPost } from "../../../lib/api";
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

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
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
  const [payAmount, setPayAmount] = useState("250.00");
  const [actionMsg, setActionMsg] = useState<string | null>(null);
  const [actionErr, setActionErr] = useState<string | null>(null);
  const [actionBusy, setActionBusy] = useState(false);
  const [newBalance, setNewBalance] = useState("");
  const [waiveAmount, setWaiveAmount] = useState("");

  const loadAll = useCallback(async () => {
    if (!loanId) return;
    setLoading(true);
    setError(null);
    try {
      // Load loan first; schedule/payments are best-effort (tolerate failures).
      const l = (await apiGet(`/lss/loans/${loanId}`)) as Loan;
      setLoan(l);
      const [sch, pay] = await Promise.allSettled([
        apiGet(`/lss/loans/${loanId}/schedule`),
        apiGet(`/lss/loans/${loanId}/payments`),
      ]);
      if (sch.status === "fulfilled") {
        setSchedule((sch.value as { schedule?: ScheduleRow[] })?.schedule ?? []);
      }
      if (pay.status === "fulfilled") {
        setPayments((pay.value as { items?: PaymentRow[] })?.items ?? []);
      }
    } catch (err) {
      setError(errMsg(err, "Could not load this loan."));
      setLoan(null);
    } finally {
      setLoading(false);
    }
  }, [loanId]);

  useEffect(() => {
    loadAll();
  }, [loadAll]);

  // Refresh only balance + payment history after an action.
  const refreshBalanceAndHistory = useCallback(async () => {
    if (!loanId) return;
    const [bal, pay] = await Promise.allSettled([
      apiGet(`/lss/accounts/${loanId}/balance`),
      apiGet(`/lss/loans/${loanId}/payments`),
    ]);
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
  }, [loanId]);

  async function makePayment() {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // NOTE: no idempotency key — a retry double-charges.
      await apiPost("/payments", {
        loan_id: loanId,
        pan: "4111111111111111", // hardcoded test card PAN (texture)
        cvv: "123", // hardcoded test CVV (texture)
        amount: parseFloat(payAmount || "0"),
        method: "card",
      });
      setActionMsg(`Payment of ${usd(payAmount)} submitted.`);
      await refreshBalanceAndHistory();
    } catch (err) {
      setActionErr(errMsg(err, "Payment failed."));
    } finally {
      setActionBusy(false);
    }
  }

  async function adjustBalance() {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // weak authz: any authenticated user can do this
      await apiPost(`/lss/accounts/${loanId}/adjust-balance`, {
        new_balance: parseFloat(newBalance || "0"),
      });
      setActionMsg(`Balance adjusted to ${usd(newBalance)}.`);
      await refreshBalanceAndHistory();
    } catch (err) {
      setActionErr(errMsg(err, "Balance adjustment failed."));
    } finally {
      setActionBusy(false);
    }
  }

  async function waiveFee() {
    setActionBusy(true);
    setActionErr(null);
    setActionMsg(null);
    try {
      // weak authz: any authenticated user can do this
      await apiPost(`/lss/accounts/${loanId}/waive-fee`, {
        amount: parseFloat(waiveAmount || "0"),
      });
      setActionMsg(`Fee of ${usd(waiveAmount)} waived.`);
      await refreshBalanceAndHistory();
    } catch (err) {
      setActionErr(errMsg(err, "Fee waiver failed."));
    } finally {
      setActionBusy(false);
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
          <button onClick={makePayment} disabled={actionBusy}>
            {actionBusy ? "Processing…" : "Pay with card on file"}
          </button>
        </div>
        <p className="hint" style={{ marginTop: 10 }}>
          Charged to card ending 1111. Payments post immediately.
        </p>
      </div>

      {/* Rep actions — shown to ANY authenticated user (weak-authz texture) */}
      {/* weak authz: any authenticated user can do this */}
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
    </main>
  );
}
