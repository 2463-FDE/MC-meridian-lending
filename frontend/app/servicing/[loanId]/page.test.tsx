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
