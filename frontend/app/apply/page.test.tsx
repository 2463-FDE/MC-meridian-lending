// Regression test for the applicant acceptance gate (PR review): acceptance used to enable
// on a bare `disclosure_status === "delivered"` flag, without the borrower ever seeing the
// immutable TILA document that flag claims was delivered. The boarding guard was a status
// flag rather than evidence the borrower received the disclosure. These assert on what the
// applicant actually sees: the delivered document body must render, and the Accept control
// must stay held until that document has been fetched.

import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import ApplyPage from "./page";

vi.mock("next/link", () => ({
  default: ({ children }: { children?: ReactNode }) => <a>{children}</a>,
}));

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("../../lib/api", () => ({
  apiGet: (...args: unknown[]) => apiGet(...args),
  apiPost: (...args: unknown[]) => apiPost(...args),
}));

// A resumed, approved application with an offer — reached via the resume effect so the test
// starts on step 5 with the offer already on screen, without driving the whole wizard.
const RESUMED_APP = {
  status: "offer",
  kyc: {
    name_verified: true,
    dob_verified: true,
    address_verified: true,
    ssn_verified: true,
  },
  decision: "approve",
  offer: {
    apr: 9.584,
    finance_charge: 3628.71,
    monthly_payment: 439.35,
    amount_financed: 17460.0,
    total_of_payments: 21088.71,
  },
};

const STORED_DOCUMENT = {
  heading: "Federal Truth-in-Lending Disclosure",
  figures: {
    apr: "9.584",
    finance_charge: "3628.71",
    amount_financed: "17460.00",
    total_of_payments: "21088.71",
    monthly_payment: "439.35",
  },
  payment_terms: "You will make equal monthly payments until the loan is repaid.",
  prepayment: "You may repay early without a penalty.",
};

// jsdom here does not provide Storage; the page guards every access, so a minimal
// in-memory stand-in is enough to exercise the resume path the test drives through.
function installLocalStorage() {
  const store = new Map<string, string>();
  const mock = {
    getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
    setItem: (k: string, v: string) => void store.set(k, String(v)),
    removeItem: (k: string) => void store.delete(k),
    clear: () => store.clear(),
  };
  Object.defineProperty(window, "localStorage", {
    value: mock,
    configurable: true,
  });
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  installLocalStorage();
  window.localStorage.setItem(
    "meridian:apply:resume",
    JSON.stringify({ app_id: 1 })
  );
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("apply — acceptance gates on the delivered document, not a status flag", () => {
  it("renders the delivered TILA document and enables Accept once it is fetched", async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path === "/los/applications/1") return RESUMED_APP;
      if (path === "/los/applications/1/disclosure")
        return { disclosure_status: "delivered" };
      if (path === "/los/applications/1/disclosure/document")
        return STORED_DOCUMENT;
      throw new Error(`unexpected GET ${path}`);
    });

    render(<ApplyPage />);

    // The delivered document's borrower-facing prose is on screen...
    await screen.findByText(STORED_DOCUMENT.payment_terms);
    expect(screen.getByText(STORED_DOCUMENT.prepayment)).toBeTruthy();
    // ...and only then is Accept offered.
    expect(
      screen.getByRole("button", { name: /accept offer/i })
    ).toBeTruthy();
  });

  it("holds Accept when the disclosure is not delivered (no document to show)", async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path === "/los/applications/1") return RESUMED_APP;
      if (path === "/los/applications/1/disclosure")
        return { disclosure_status: "draft" };
      // The document read must never be reached for an undelivered disclosure.
      throw new Error(`unexpected GET ${path}`);
    });

    render(<ApplyPage />);

    await screen.findByText(/being finalised/i);
    expect(screen.queryByRole("button", { name: /accept offer/i })).toBeNull();
    await waitFor(() =>
      expect(
        apiGet.mock.calls.some(
          (c) => c[0] === "/los/applications/1/disclosure/document"
        )
      ).toBe(false)
    );
  });
});
