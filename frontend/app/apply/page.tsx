"use client";

import { useState } from "react";
import { apiPost } from "../../lib/api";

export default function ApplyPage() {
  const [form, setForm] = useState({
    name: "",
    dob: "",
    ssn: "",
    address: "",
    income: "",
    amount: "",
    term_months: "36",
    purpose: "personal",
  });
  const [result, setResult] = useState<unknown>(null);
  const [busy, setBusy] = useState(false);

  function set(k: string, v: string) {
    setForm((f) => ({ ...f, [k]: v }));
  }

  async function submit() {
    setBusy(true);
    try {
      const res = await apiPost("/los/applications", {
        ...form,
        income: parseFloat(form.income || "0"),
        amount: parseFloat(form.amount || "0"),
        term_months: parseInt(form.term_months, 10),
      });
      setResult(res);
    } catch (e) {
      setResult({ error: String(e) });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="wrap">
      <h1>Loan Application</h1>
      <p className="sub">Personal installment loan — $1,000 to $50,000</p>

      <div className="card">
        <label>Full name</label>
        <input value={form.name} onChange={(e) => set("name", e.target.value)} />

        <label>Date of birth</label>
        <input type="date" value={form.dob} onChange={(e) => set("dob", e.target.value)} />

        <label>SSN</label>
        <input value={form.ssn} onChange={(e) => set("ssn", e.target.value)} placeholder="###-##-####" />

        <label>Address</label>
        <input value={form.address} onChange={(e) => set("address", e.target.value)} />

        <label>Annual income</label>
        <input value={form.income} onChange={(e) => set("income", e.target.value)} />

        <label>Loan amount</label>
        <input value={form.amount} onChange={(e) => set("amount", e.target.value)} />

        <label>Term (months)</label>
        <select value={form.term_months} onChange={(e) => set("term_months", e.target.value)}>
          <option>12</option><option>24</option><option>36</option>
          <option>48</option><option>60</option>
        </select>

        <button onClick={submit} disabled={busy}>
          {busy ? "Submitting…" : "Submit application"}
        </button>
      </div>

      {result ? (
        <>
          <h2>Result</h2>
          <pre>{JSON.stringify(result, null, 2)}</pre>
        </>
      ) : null}
    </main>
  );
}
