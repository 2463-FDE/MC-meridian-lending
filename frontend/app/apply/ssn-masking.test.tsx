// The SSN the applicant types was rendered as a plain text input, and the review step echoed
// all nine digits back. Both put the number on screen for anyone watching the applicant --
// shoulder, screen share, recorded demo. These assert the masking that closes that: the input
// is a password field with an opt-in reveal that does not survive leaving the step, and the
// review step shows only the last four.

import type { ReactNode } from "react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import ApplyPage from "./page";
import { maskSsn } from "../../lib/format";

vi.mock("next/link", () => ({
  default: ({ children }: { children?: ReactNode }) => <a>{children}</a>,
}));

const apiGet = vi.fn();
const apiPost = vi.fn();

vi.mock("../../lib/api", () => ({
  apiGet: (...args: unknown[]) => apiGet(...args),
  apiPost: (...args: unknown[]) => apiPost(...args),
}));

// jsdom provides no Storage; the page guards every access. A minimal stand-in with nothing
// saved keeps the resume effect from firing, so the wizard renders at step 1.
function installEmptyLocalStorage() {
  const store = new Map<string, string>();
  Object.defineProperty(window, "localStorage", {
    value: {
      getItem: (k: string) => (store.has(k) ? store.get(k)! : null),
      setItem: (k: string, v: string) => void store.set(k, String(v)),
      removeItem: (k: string) => void store.delete(k),
      clear: () => store.clear(),
    },
    configurable: true,
  });
}

function ssnInput(): HTMLInputElement {
  return screen.getByPlaceholderText("###-##-####") as HTMLInputElement;
}

beforeEach(() => {
  apiGet.mockReset();
  apiPost.mockReset();
  installEmptyLocalStorage();
});

afterEach(() => {
  cleanup();
  window.localStorage.clear();
});

describe("apply — the SSN is masked while it is being entered", () => {
  it("renders the SSN field as a password input", () => {
    render(<ApplyPage />);
    expect(ssnInput().type).toBe("password");
  });

  it("reveals on the toggle and hides again on a second press", () => {
    render(<ApplyPage />);

    fireEvent.click(
      screen.getByRole("button", { name: /show social security number/i })
    );
    expect(ssnInput().type).toBe("text");

    fireEvent.click(
      screen.getByRole("button", { name: /hide social security number/i })
    );
    expect(ssnInput().type).toBe("password");
  });

  it("re-hides a revealed SSN when the wizard leaves step 1", () => {
    render(<ApplyPage />);

    fireEvent.change(ssnInput(), { target: { value: "123-45-6789" } });
    fireEvent.click(
      screen.getByRole("button", { name: /show social security number/i })
    );
    expect(ssnInput().type).toBe("text");

    // Next fails validation (the rest of step 1 is empty) so the wizard stays put; Back from
    // step 1 is disabled. Drive the step through a field the applicant controls instead:
    // filling step 1 and advancing must not carry the reveal forward.
    fireEvent.change(screen.getByPlaceholderText("Jane Q. Borrower"), {
      target: { value: "Jane Q. Borrower" },
    });
    fireEvent.change(screen.getByPlaceholderText("you@example.com"), {
      target: { value: "jane@example.com" },
    });
    fireEvent.change(screen.getByPlaceholderText("(555) 555-0123"), {
      target: { value: "5555550123" },
    });
    fireEvent.change(
      screen.getByPlaceholderText("123 Main St, Springfield, IL 62704"),
      { target: { value: "1 Main St" } }
    );
    fireEvent.change(document.querySelector('input[type="date"]')!, {
      target: { value: "1990-04-22" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Next" }));

    // Step 2 is on screen; walking back to step 1 shows a masked field again.
    expect(screen.getByText(/Step 2 · Employment & income/)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Back" }));
    expect(ssnInput().type).toBe("password");
  });
});

describe("maskSsn — the review step shows only the last four", () => {
  it("masks a complete SSN down to the last four digits", () => {
    expect(maskSsn("123-45-6789")).toBe("***-**-6789");
    expect(maskSsn("123456789")).toBe("***-**-6789");
  });

  it("fully masks anything that is not nine digits, rather than echoing a prefix", () => {
    // A half-typed value must not leak the digits already entered.
    expect(maskSsn("123-45")).toBe("•••••••••");
    expect(maskSsn("1234567890")).toBe("•••••••••");
    expect(maskSsn("abc")).toBe("•••••••••");
  });

  it("passes an empty value through so the review row renders its own placeholder", () => {
    expect(maskSsn("")).toBe("");
  });
});
