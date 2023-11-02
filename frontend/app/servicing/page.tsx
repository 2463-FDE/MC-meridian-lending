"use client";

import { useState } from "react";
import { apiGet, apiPost } from "../../lib/api";

export default function ServicingPage() {
  const [loanId, setLoanId] = useState("4471");
  const [balance, setBalance] = useState<unknown>(null);
  const [payResult, setPayResult] = useState<unknown>(null);
  const [amount, setAmount] = useState("250.00");

  async function loadBalance() {
    setBalance(await apiGet(`/lss/accounts/${loanId}/balance`));
  }

  async function makePayment() {
    // NOTE: no idempotency key sent. A retry will double-charge.
    setPayResult(
      await apiPost("/lss/payments", {
        loan_id: parseInt(loanId, 10),
        pan: "4111111111111111",
        cvv: "123",
        amount: parseFloat(amount),
        method: "card",
      })
    );
  }

  return (
    <main className="wrap">
      <h1>Servicing Dashboard</h1>
      <p className="sub">Manage a loan account — reps can adjust balances and waive fees.</p>

      <div className="card">
        <label>Loan ID</label>
        <input value={loanId} onChange={(e) => setLoanId(e.target.value)} />
        <button onClick={loadBalance}>Load balance</button>

        {balance ? (
          <>
            <h2>Balance</h2>
            <pre>{JSON.stringify(balance, null, 2)}</pre>
          </>
        ) : null}

        <h2>Make a payment</h2>
        <label>Amount</label>
        <input value={amount} onChange={(e) => setAmount(e.target.value)} />
        <button onClick={makePayment}>Pay</button>

        {payResult ? <pre>{JSON.stringify(payResult, null, 2)}</pre> : null}
      </div>
    </main>
  );
}
