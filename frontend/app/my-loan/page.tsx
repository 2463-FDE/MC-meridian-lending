"use client";

import Link from "next/link";
import { useCallback, useEffect, useState } from "react";
import StatusChip from "../../components/StatusChip";
import { apiGet } from "../../lib/api";
import { usd, pct, shortDate } from "../../lib/format";

interface LoanRow {
  id: string | number;
  applicant_name: string;
  borrower?: string;
  principal: number;
  apr: number;
  term_months: number;
  status: string;
  balance: number;
  past_due: number;
  opened_at: string;
}

interface LoansResponse {
  items: LoanRow[];
  total: number;
  limit: number;
  offset: number;
}

function errMsg(err: unknown, fallback: string): string {
  if (err && typeof err === "object" && "detail" in err) {
    return String((err as { detail: unknown }).detail) || fallback;
  }
  if (err instanceof Error) return err.message;
  return fallback;
}

export default function MyLoanPage() {
  const [items, setItems] = useState<LoanRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      // Server-scoped to the caller's own applicant_id (ADR 0014 Decision 1) --
      // no client-side filtering needed, and no borrower can see another
      // borrower's loans through this call.
      const res = (await apiGet(`/lss/loans/mine`)) as LoansResponse;
      setItems(res.items ?? []);
    } catch (err) {
      setError(errMsg(err, "Could not load your loans."));
      setItems([]);
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  return (
    <main className="wrap">
      <div className="spread">
        <div>
          <h1>My loan</h1>
          <p className="sub" style={{ margin: 0 }}>
            Your balance, terms, and account activity.
          </p>
        </div>
        <button className="btn-ghost" onClick={load} disabled={loading}>
          {loading ? "Refreshing…" : "Refresh"}
        </button>
      </div>

      {error ? <div className="alert alert-error">{error}</div> : null}

      {loading && items.length === 0 ? (
        <p className="muted" style={{ marginTop: 20 }}>
          Loading your loans…
        </p>
      ) : items.length === 0 && !error ? (
        <div className="card" style={{ marginTop: 20 }}>
          <h3 style={{ marginBottom: 6 }}>No loans found under your account</h3>
          <p className="muted" style={{ marginTop: 0 }}>
            If you haven&apos;t applied yet, check your rate and apply in a few
            minutes. Already have a loan but don&apos;t see it here? Contact
            support to link it to your account.
          </p>
          <Link href="/apply" className="btn" style={{ marginTop: 8 }}>
            Apply for a loan
          </Link>
        </div>
      ) : (
        <>
          <div className="grid grid-2" style={{ marginTop: 20 }}>
            {items.map((l) => (
              <div className="card" key={String(l.id)}>
                <div className="spread" style={{ marginBottom: 12 }}>
                  <div>
                    <div className="card-title">Loan #{String(l.id)}</div>
                    <p className="muted" style={{ margin: "4px 0 0" }}>
                      {l.applicant_name || l.borrower}
                    </p>
                  </div>
                  <StatusChip status={l.status} />
                </div>

                <div className="dl">
                  <div className="dl-row">
                    <dt>Current balance</dt>
                    <dd>{usd(l.balance)}</dd>
                  </div>
                  <div className="dl-row">
                    <dt>Past due</dt>
                    <dd className={l.past_due > 0 ? "danger-text" : ""}>
                      {usd(l.past_due)}
                    </dd>
                  </div>
                  <div className="dl-row">
                    <dt>Original principal</dt>
                    <dd>{usd(l.principal)}</dd>
                  </div>
                  <div className="dl-row">
                    <dt>APR</dt>
                    <dd>{pct(l.apr)}</dd>
                  </div>
                  <div className="dl-row">
                    <dt>Term</dt>
                    <dd>{l.term_months} months</dd>
                  </div>
                  <div className="dl-row">
                    <dt>Opened</dt>
                    <dd>{shortDate(l.opened_at)}</dd>
                  </div>
                </div>

                <Link
                  href={`/servicing/${l.id}`}
                  className="btn btn-block"
                  style={{ marginTop: 16 }}
                >
                  View account & make a payment
                </Link>
              </div>
            ))}
          </div>
        </>
      )}
    </main>
  );
}
