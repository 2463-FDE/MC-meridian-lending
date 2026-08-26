// Regression tests for per-loan state leaking across /servicing/[loanId] navigations
// (PR review sweep — same defect class as the underwriting detail page). This component
// is reused across loan detail routes, so without a per-loan reset and a staleness guard
// one borrower's account facts appear on another's screen:
//
//  1. Balance, payment history and the last action result carried over on navigation,
//     and the money-action amount inputs stayed pre-filled from the previous loan.
//  2. A payment started on one loan could still be in flight when the CSR opened
//     another, and its confirmation landed on the new account.

import type { ReactNode } from "react";
import { useLayoutEffect } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
} from "@testing-library/react";
import LoanDetailPage from "./page";

let routeLoanId = "1";

vi.mock("next/navigation", () => ({
  useParams: () => ({ loanId: routeLoanId }),
}));

vi.mock("next/link", () => ({
  default: ({ children }: { children?: ReactNode }) => <a>{children}</a>,
}));

const apiGet = vi.fn();
const apiPost = vi.fn();
const getUser = vi.fn();

vi.mock("../../../lib/api", () => ({
  apiGet: (...args: unknown[]) => apiGet(...args),
  apiPost: (...args: unknown[]) => apiPost(...args),
  getUser: () => getUser(),
}));

// Records the DOM of every commit, before the browser would paint it. useLayoutEffect
// fires after React writes the commit to the DOM but ahead of paint, so each frame it
// captures is something a CSR could have seen. A reset performed in useEffect runs AFTER
// paint, so asserting only on the settled DOM cannot tell whether a stale frame was shown
// on the way there. No dependency array, so it runs on every commit.
function PaintProbe({ frames }: { frames: string[] }) {
  useLayoutEffect(() => {
    frames.push(document.body.textContent ?? "");
  });
  return null;
}

const LOAN_1 = {
  id: 1,
  applicant_name: "Maria Alvarez",
  principal: 12000,
  apr: 7.99,
  term_months: 48,
  status: "current",
  balance: 9000,
  past_due: 0,
  opened_at: "2026-01-05T00:00:00Z",
};

const LOAN_2 = {
  ...LOAN_1,
  id: 2,
  applicant_name: "Dan Brown",
  balance: 4200,
};

beforeEach(() => {
  routeLoanId = "1";
  apiGet.mockReset();
  apiPost.mockReset();
  getUser.mockReset();
  getUser.mockReturnValue({ role: "csr" });
  apiGet.mockImplementation(async (path: string) => {
    if (path === "/lss/loans/1") return LOAN_1;
    if (path === "/lss/loans/2") return LOAN_2;
    if (path.endsWith("/schedule")) return { schedule: [] };
    if (path.endsWith("/payments")) return { items: [] };
    if (path.includes("/balance")) return { balance: 8750, past_due: 0 };
    throw new Error(`unexpected GET ${path}`);
  });
});

afterEach(() => {
  cleanup();
});

describe("loan detail — per-loan state scoping", () => {
  it("clears the previous loan's action result and amount inputs on navigation", async () => {
    apiPost.mockResolvedValue({ ok: true });

    const view = render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    // The pay-amount field is the first number input on the page (the adjust-balance and
    // waive-fee fields follow it); its label is not associated with it in the markup.
    const amount = screen.getAllByRole("spinbutton")[0] as HTMLInputElement;
    fireEvent.change(amount, { target: { value: "999.99" } });
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Payment of $999.99 submitted.");

    routeLoanId = "2";
    view.rerender(<LoanDetailPage />);
    await screen.findByText("Dan Brown");

    // The confirmation names an amount paid against loan 1; it must not follow the CSR.
    expect(screen.queryByText("Payment of $999.99 submitted.")).toBeNull();
    // Nor may the amount typed for loan 1 stay armed in loan 2's pay field.
    expect(
      (screen.getAllByRole("spinbutton")[0] as HTMLInputElement).value
    ).toBe("250.00");
  });

  it("never commits the previous borrower beside the new loan number", async () => {
    const frames: string[] = [];
    let resolveLoan2: (value: unknown) => void = () => {};
    const pendingLoan2 = new Promise((resolve) => {
      resolveLoan2 = resolve;
    });
    apiGet.mockImplementation((path: string) => {
      if (path === "/lss/loans/1") return Promise.resolve(LOAN_1);
      if (path === "/lss/loans/2") return pendingLoan2;
      if (path.endsWith("/schedule")) return Promise.resolve({ schedule: [] });
      if (path.endsWith("/payments")) return Promise.resolve({ items: [] });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    const view = render(
      <>
        <LoanDetailPage />
        <PaintProbe frames={frames} />
      </>
    );
    await screen.findByText("Maria Alvarez");

    // Only the frames produced by the navigation itself are under test.
    frames.length = 0;
    routeLoanId = "2";
    view.rerender(
      <>
        <LoanDetailPage />
        <PaintProbe frames={frames} />
      </>
    );

    // No committed frame may pair loan 1's borrower or balance with loan 2's header.
    const leaked = frames.filter(
      (f) =>
        f.includes("Loan #2") &&
        (f.includes("Maria Alvarez") || f.includes("$9,000.00"))
    );
    expect(leaked).toEqual([]);

    await act(async () => {
      resolveLoan2(LOAN_2);
    });
    await screen.findByText("Dan Brown");
  });

  it("hides the previous borrower while the next loan is still loading", async () => {
    let resolveLoan2: (value: unknown) => void = () => {};
    const pendingLoan2 = new Promise((resolve) => {
      resolveLoan2 = resolve;
    });
    apiGet.mockImplementation((path: string) => {
      if (path === "/lss/loans/1") return Promise.resolve(LOAN_1);
      if (path === "/lss/loans/2") return pendingLoan2;
      if (path.endsWith("/schedule")) return Promise.resolve({ schedule: [] });
      if (path.endsWith("/payments")) return Promise.resolve({ items: [] });
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    const view = render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    // Navigate to loan 2 and hold its load open.
    routeLoanId = "2";
    view.rerender(<LoanDetailPage />);

    // Loan 1's borrower and balance must not sit under loan 2's header.
    expect(screen.queryByText("Maria Alvarez")).toBeNull();
    expect(screen.queryByText("$9,000.00")).toBeNull();
    expect(screen.getByText(/Loading loan #2/)).toBeTruthy();

    await act(async () => {
      resolveLoan2(LOAN_2);
    });
    await screen.findByText("Dan Brown");
  });

  it("drops an in-flight payment confirmation after the route moves to another loan", async () => {
    let resolvePayment: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      resolvePayment = resolve;
    });
    apiPost.mockImplementation((path: string) => {
      if (path === "/payments") return pending;
      throw new Error(`unexpected POST ${path}`);
    });

    const view = render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );

    routeLoanId = "2";
    view.rerender(<LoanDetailPage />);
    await screen.findByText("Dan Brown");

    await act(async () => {
      resolvePayment({ ok: true });
    });

    expect(screen.queryByText("Payment of $250.00 submitted.")).toBeNull();
  });
});

describe("loan detail — captured but unapplied payment", () => {
  it("does not show a success message when the apply is rejected", async () => {
    // The backend now returns a non-2xx (424 Failed Dependency, chosen over
    // 502/503/504 so generic retry logic does not treat this as transient)
    // with a captured_unapplied detail instead of a plain 200 (Codex review,
    // PR 32) -- apiPost throws on any non-2xx, so this must land in the catch
    // branch, never the success one.
    // Shape mirrors ApiError from lib/api: a status alongside the detail.
    apiPost.mockRejectedValue({
      status: 424,
      detail:
        "Payment captured (payment_id=42) but could not be applied to your " +
        "balance. Do not retry -- contact support to reconcile this payment.",
    });

    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );

    await screen.findByText(/could not be applied to your balance/);
    expect(screen.queryByText(/submitted\.$/)).toBeNull();
    // 424 is the one non-retryable failure: the charge captured, so replaying the
    // key returns that same captured payment and still leaves the balance uncredited.
    // Offering "Retry same charge" here would contradict the message's own
    // "Do not retry -- contact support" instruction.
    expect(
      screen.queryByRole("button", { name: "Retry same charge" })
    ).toBeNull();
  });
});

describe("loan detail — payment idempotency (D19)", () => {
  it("sends an Idempotency-Key header on every payment submission", async () => {
    apiPost.mockResolvedValue({ ok: true });

    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Payment of $250.00 submitted.");

    expect(apiPost).toHaveBeenCalledTimes(1);
    const [path, , headers] = apiPost.mock.calls[0] as [
      string,
      unknown,
      Record<string, string> | undefined,
    ];
    expect(path).toBe("/payments");
    expect(typeof headers?.["Idempotency-Key"]).toBe("string");
    expect(headers?.["Idempotency-Key"].length).toBeGreaterThan(0);
  });

  it("retries with the exact same Idempotency-Key and body, not a fresh one", async () => {
    apiPost.mockResolvedValue({ ok: true });

    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Payment of $250.00 submitted.");

    fireEvent.click(screen.getByRole("button", { name: "Retry same charge" }));
    await screen.findByText(/collapsed to the original payment/);

    expect(apiPost).toHaveBeenCalledTimes(2);
    const [, firstBody, firstHeaders] = apiPost.mock.calls[0];
    const [, secondBody, secondHeaders] = apiPost.mock.calls[1];
    expect(secondHeaders).toEqual(firstHeaders);
    expect(secondBody).toEqual(firstBody);
  });

  it("keeps the original key retryable after an ambiguous failure", async () => {
    // A network error, timeout or 5xx leaves the outcome unknown -- the request
    // may have reached the processor and captured. The key must already be
    // recorded when the send fails, or the retry affordance never renders and
    // the borrower's next Pay click mints a second intent under a fresh key,
    // which is the double charge claim_or_branch() exists to collapse.
    apiPost.mockRejectedValueOnce(new Error("Network request failed"));

    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Network request failed");

    apiPost.mockResolvedValueOnce({ ok: true });
    fireEvent.click(screen.getByRole("button", { name: "Retry same charge" }));
    await screen.findByText(/collapsed to the original payment/);

    expect(apiPost).toHaveBeenCalledTimes(2);
    const [, firstBody, firstHeaders] = apiPost.mock.calls[0];
    const [, secondBody, secondHeaders] = apiPost.mock.calls[1];
    expect(secondHeaders).toEqual(firstHeaders);
    expect(secondBody).toEqual(firstBody);
  });
  // The primary Pay button is the default post-failure path, so it is the one that
  // decides whether an unresolved charge becomes a second capture. While the last
  // send is unresolved AND the form still describes that same charge, it must not
  // mint a fresh key -- the borrower reaches a new intent by resetting or by editing
  // the amount, both deliberate acts.
  async function payAfterAmbiguousFailure() {
    apiPost.mockRejectedValueOnce(new Error("Network request failed"));
    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Network request failed");
  }

  it("does not mint a second key when Pay is clicked after an ambiguous failure", async () => {
    await payAfterAmbiguousFailure();

    const pay = screen.getByRole("button", { name: "Pay with card on file" });
    expect(pay.hasAttribute("disabled")).toBe(true);

    fireEvent.click(pay);
    expect(apiPost).toHaveBeenCalledTimes(1);
  });

  it("mints a fresh key only after an explicit new-payment reset", async () => {
    await payAfterAmbiguousFailure();

    fireEvent.click(screen.getByRole("button", { name: "Start a new payment" }));
    apiPost.mockResolvedValueOnce({ ok: true });
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Payment of $250.00 submitted.");

    expect(apiPost).toHaveBeenCalledTimes(2);
    const [, , firstHeaders] = apiPost.mock.calls[0];
    const [, , secondHeaders] = apiPost.mock.calls[1];
    expect(secondHeaders?.["Idempotency-Key"]).not.toBe(
      firstHeaders?.["Idempotency-Key"]
    );
  });

  it("re-enables Pay with a fresh key once the amount is edited", async () => {
    await payAfterAmbiguousFailure();

    // Editing the amount describes a different charge, so it is no longer the
    // unresolved one and needs its own key.
    // Same selector the per-loan scoping tests use -- the pay amount is the first
    // number input on the page.
    fireEvent.change(screen.getAllByRole("spinbutton")[0], {
      target: { value: "125.00" },
    });
    apiPost.mockResolvedValueOnce({ ok: true });
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Payment of $125.00 submitted.");

    expect(apiPost).toHaveBeenCalledTimes(2);
    const [, firstBody, firstHeaders] = apiPost.mock.calls[0];
    const [, secondBody, secondHeaders] = apiPost.mock.calls[1];
    expect(secondHeaders?.["Idempotency-Key"]).not.toBe(
      firstHeaders?.["Idempotency-Key"]
    );
    expect((secondBody as { amount: number }).amount).toBe(125);
    expect((firstBody as { amount: number }).amount).toBe(250);
  });

  it("still allows a genuine second identical payment after one succeeds", async () => {
    // The gate is scoped to an UNRESOLVED send. A payment that came back 2xx is
    // resolved, so a borrower paying $250 twice on purpose is not blocked and gets
    // a second key -- the case that ruled out reusing the key on a matching body.
    apiPost.mockResolvedValue({ ok: true });
    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");

    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText("Payment of $250.00 submitted.");
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );

    expect(apiPost).toHaveBeenCalledTimes(2);
    const [, , firstHeaders] = apiPost.mock.calls[0];
    const [, , secondHeaders] = apiPost.mock.calls[1];
    expect(secondHeaders?.["Idempotency-Key"]).not.toBe(
      firstHeaders?.["Idempotency-Key"]
    );
  });

  it("does not warn that the charge may have gone through while it is still in flight", async () => {
    // The key is recorded before the send, so the panel renders during the flight.
    // The ambiguity warning and the reset must wait for the attempt to settle --
    // shown mid-flight they describe a failure that has not happened.
    let resolvePayment: (value: unknown) => void = () => {};
    apiPost.mockImplementation(
      () =>
        new Promise((resolve) => {
          resolvePayment = resolve;
        })
    );

    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );

    expect(screen.queryByText(/may already have gone through/)).toBeNull();
    expect(
      screen.queryByRole("button", { name: "Start a new payment" })
    ).toBeNull();

    await act(async () => {
      resolvePayment({ ok: true });
    });
    await screen.findByText("Payment of $250.00 submitted.");
  });

  it("blocks Pay from minting a fresh key after a captured-unapplied 424", async () => {
    // 424 means the card WAS charged and only the ledger apply failed. Minting a new
    // key there is an outright second capture, not an ambiguous one, so the primary
    // button stays shut until the borrower explicitly starts a new payment.
    apiPost.mockRejectedValueOnce({
      status: 424,
      detail:
        "Payment captured (payment_id=42) but could not be applied to your " +
        "balance. Do not retry -- contact support to reconcile this payment.",
    });

    render(<LoanDetailPage />);
    await screen.findByText("Maria Alvarez");
    fireEvent.click(
      screen.getByRole("button", { name: "Pay with card on file" })
    );
    await screen.findByText(/could not be applied to your balance/);

    const pay = screen.getByRole("button", { name: "Pay with card on file" });
    expect(pay.hasAttribute("disabled")).toBe(true);
    fireEvent.click(pay);
    expect(apiPost).toHaveBeenCalledTimes(1);

    // Retry stays absent -- replaying the key returns the same captured payment.
    expect(
      screen.queryByRole("button", { name: "Retry same charge" })
    ).toBeNull();
  });
});
