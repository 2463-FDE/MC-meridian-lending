// Regression tests for two ways the underwriting detail screen could show one
// applicant's regulated decision facts in the wrong place (PR review):
//
//  1. A manual "Run decision" left the previous assistant card on screen, so a
//     superseded outcome sat beside the new primary decision.
//  2. This page component is reused across /underwriting/[appId] navigations, so an
//     assistant call started on one application could still be in flight when the
//     officer opened another, and its response stamped the earlier applicant's facts
//     onto the new screen.
//
// Both assert on what the officer actually sees, not on internal state.

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
import UnderwritingDetailPage from "./page";

// Records the DOM of every commit, before the browser would paint it. useLayoutEffect
// fires after React writes the commit to the DOM but ahead of paint, so each frame it
// captures is something an officer could have seen. That is the difference this probe
// exists to measure: a reset performed in useEffect runs AFTER paint, so asserting on
// the settled DOM (what rerender leaves behind) cannot tell whether a stale frame was
// shown on the way there. No dependency array, so it runs on every commit.
function PaintProbe({ frames }: { frames: string[] }) {
  useLayoutEffect(() => {
    frames.push(document.body.textContent ?? "");
  });
  return null;
}

// The live route param. The mock reads it on every call, so assigning to it and
// re-rendering models a client-side navigation to another application.
let routeAppId = "1";

vi.mock("next/navigation", () => ({
  useParams: () => ({ appId: routeAppId }),
}));

vi.mock("next/link", () => ({
  default: ({ children }: { children?: ReactNode }) => <a>{children}</a>,
}));

const apiGet = vi.fn();
const apiPost = vi.fn();

// The factory bodies stay lazy so they resolve apiGet/apiPost at call time, after the
// consts above are initialised (vi.mock is hoisted above them).
vi.mock("../../../lib/api", () => ({
  apiGet: (...args: unknown[]) => apiGet(...args),
  apiPost: (...args: unknown[]) => apiPost(...args),
}));

const APP_1 = {
  id: 1,
  applicant: { name: "Maria Alvarez" },
  amount: 12000,
  term_months: 48,
  purpose: "debt_consolidation",
  status: "submitted",
};

const APP_2 = {
  ...APP_1,
  id: 2,
  applicant: { name: "Dan Brown" },
};

const ASSISTANT_SUMMARY = "Approved on a thin but clean file.";

function assistantResultFor(applicationId: number) {
  return {
    application_id: applicationId,
    outcome: "approved",
    score: 712,
    policy_band: "prime",
    principal_reasons: [{ code: "R1", reason: "Limited credit history" }],
    decided_by: "assistant",
    decided_at: "2026-07-01T00:00:00Z",
    summary: ASSISTANT_SUMMARY,
    narration_validated: true,
  };
}

beforeEach(() => {
  routeAppId = "1";
  apiGet.mockReset();
  apiPost.mockReset();
  apiGet.mockImplementation(async (path: string) => {
    if (path === "/los/applications/1") return APP_1;
    if (path === "/los/applications/2") return APP_2;
    throw new Error(`unexpected GET ${path}`);
  });
});

afterEach(() => {
  cleanup();
});

describe("underwriting detail — assistant panel", () => {
  it("clears the assistant card once a manual decision supersedes it", async () => {
    apiGet.mockImplementation(async (path: string) => {
      if (path === "/los/applications/1") return APP_1;
      if (path === "/los/assistant/decisions/1") return assistantResultFor(1);
      throw new Error(`unexpected GET ${path}`);
    });
    apiPost.mockResolvedValue({ app_id: 1, decision: "declined", score: 640 });

    render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");

    // Explain populates the assistant card from the recorded decision.
    fireEvent.click(screen.getByRole("button", { name: "Explain" }));
    expect(await screen.findByText(ASSISTANT_SUMMARY)).toBeTruthy();

    // A manual decision records a NEW event, so the card now describes a superseded one.
    fireEvent.click(screen.getByRole("button", { name: "Run decision" }));
    await screen.findByText("Decision recorded: declined.");

    expect(screen.queryByText(ASSISTANT_SUMMARY)).toBeNull();
  });

  it("drops an in-flight assistant response after the route moves to another application", async () => {
    let resolveAssistant: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      resolveAssistant = resolve;
    });
    apiPost.mockImplementation((path: string) => {
      if (path === "/los/assistant/decisions/1") return pending;
      throw new Error(`unexpected POST ${path}`);
    });

    const view = render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");

    // Start the assistant run on application 1 and leave it unresolved.
    fireEvent.click(screen.getByRole("button", { name: "Run AI assistant" }));

    // Navigate to application 2 while that call is still in flight.
    routeAppId = "2";
    view.rerender(<UnderwritingDetailPage />);
    await screen.findAllByText("Dan Brown");

    // Application 1's response lands on application 2's screen.
    await act(async () => {
      resolveAssistant(assistantResultFor(1));
    });

    expect(screen.queryByText(ASSISTANT_SUMMARY)).toBeNull();
    expect(screen.queryByText(/Limited credit history/)).toBeNull();
  });

  it("never commits the previous applicant beside the new application number", async () => {
    const frames: string[] = [];
    let resolveApp2: (value: unknown) => void = () => {};
    const pendingApp2 = new Promise((resolve) => {
      resolveApp2 = resolve;
    });
    apiGet.mockImplementation((path: string) => {
      if (path === "/los/applications/1") {
        return Promise.resolve({ ...APP_1, decision: "declined" });
      }
      if (path === "/los/applications/2") return pendingApp2;
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    const view = render(
      <>
        <UnderwritingDetailPage />
        <PaintProbe frames={frames} />
      </>
    );
    await screen.findAllByText("Maria Alvarez");

    // Only the frames produced by the navigation itself are under test.
    frames.length = 0;
    routeAppId = "2";
    view.rerender(
      <>
        <UnderwritingDetailPage />
        <PaintProbe frames={frames} />
      </>
    );

    // No committed frame may pair application 1's applicant or decision with
    // application 2's header.
    const leaked = frames.filter(
      (f) =>
        f.includes("Application #2") &&
        (f.includes("Maria Alvarez") || f.includes("Declined"))
    );
    expect(leaked).toEqual([]);

    await act(async () => {
      resolveApp2(APP_2);
    });
    await screen.findAllByText("Dan Brown");
  });

  it("hides the previous applicant while the next application is still loading", async () => {
    let resolveApp2: (value: unknown) => void = () => {};
    const pendingApp2 = new Promise((resolve) => {
      resolveApp2 = resolve;
    });
    apiGet.mockImplementation((path: string) => {
      // Application 1 carries a recorded decision, the regulated fact that must not
      // survive the navigation. Application 2's load never settles.
      if (path === "/los/applications/1") {
        return Promise.resolve({ ...APP_1, decision: "declined" });
      }
      if (path === "/los/applications/2") return pendingApp2;
      return Promise.reject(new Error(`unexpected GET ${path}`));
    });

    const view = render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");
    expect(screen.getByText("Declined")).toBeTruthy();

    // Navigate to application 2 and hold its load open.
    routeAppId = "2";
    view.rerender(<UnderwritingDetailPage />);

    // Application 1's applicant and decision must not sit under application 2's header.
    expect(screen.queryAllByText("Maria Alvarez")).toHaveLength(0);
    expect(screen.queryByText("Declined")).toBeNull();
    expect(screen.getByText(/Loading application #2/)).toBeTruthy();

    await act(async () => {
      resolveApp2(APP_2);
    });
    await screen.findAllByText("Dan Brown");
  });

  it("drops an in-flight assistant response after navigating away and back", async () => {
    let resolveAssistant: (value: unknown) => void = () => {};
    const pending = new Promise((resolve) => {
      resolveAssistant = resolve;
    });
    apiPost.mockImplementation((path: string) => {
      if (path === "/los/assistant/decisions/1") return pending;
      throw new Error(`unexpected POST ${path}`);
    });

    const view = render(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");
    fireEvent.click(screen.getByRole("button", { name: "Run AI assistant" }));

    // Away to application 2, then straight back to 1, still in flight. The response now
    // carries the id of the application on screen, so an id comparison would accept it;
    // it still predates the navigation that cleared the panel.
    routeAppId = "2";
    view.rerender(<UnderwritingDetailPage />);
    await screen.findAllByText("Dan Brown");
    routeAppId = "1";
    view.rerender(<UnderwritingDetailPage />);
    await screen.findAllByText("Maria Alvarez");

    await act(async () => {
      resolveAssistant(assistantResultFor(1));
    });

    expect(screen.queryByText(ASSISTANT_SUMMARY)).toBeNull();
  });
});

// The TILA disclosure panel's repair path. disclosure-service's idempotent replay records a
// document on an existing non-delivered disclosure that has none, but this screen disabled
// "Generate disclosure" the moment any disclosure existed and told the officer the row
// needed an operator. Delivery stays refused for want of the document, so the application
// could not move in either direction — the backend had the remedy and the only human who
// would ask for it was told none existed.
const PROVENANCE_BASE = {
  disclosure_id: 9,
  offer_id: 11,
  decision_event_id: 7,
  application_id: 1,
  applicant_id: 3,
  disclosed_apr: "9.584",
  chain_complete: true,
  missing_edges: [],
};

function disclosureGet(provenance: unknown, document: unknown) {
  return async (path: string) => {
    if (path === "/los/applications/1") return APP_1;
    if (path === "/los/applications/1/disclosure") {
      if (provenance === null) throw new Error("no disclosure");
      return provenance;
    }
    if (path === "/los/applications/1/disclosure/document") {
      // The 404 disclosure-service returns for a row with no document recorded.
      if (document === null) throw new Error("no document recorded");
      return document;
    }
    throw new Error(`unexpected GET ${path}`);
  };
}

const GOOD_DOCUMENT = {
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

describe("underwriting detail — disclosure document repair", () => {
  it("offers the repair when a draft disclosure has no recorded document", async () => {
    apiGet.mockImplementation(
      disclosureGet({ ...PROVENANCE_BASE, disclosure_status: "draft" }, null)
    );
    apiPost.mockResolvedValue({ status: "ok" });

    render(<UnderwritingDetailPage />);
    const repair = await screen.findByRole("button", {
      name: "Record missing document",
    });
    expect(repair.hasAttribute("disabled")).toBe(false);

    await act(async () => {
      fireEvent.click(repair);
    });
    expect(
      apiPost.mock.calls.some(
        (c) => c[0] === "/los/applications/1/disclosure"
      )
    ).toBe(true);
  });

  it("keeps generation closed once a disclosure has its document", async () => {
    apiGet.mockImplementation(
      disclosureGet(
        { ...PROVENANCE_BASE, disclosure_status: "draft" },
        GOOD_DOCUMENT
      )
    );

    render(<UnderwritingDetailPage />);
    const button = await screen.findByRole("button", {
      name: "Generate disclosure",
    });
    expect(button.hasAttribute("disabled")).toBe(true);
  });

  it("offers no repair for a delivered row, which is frozen", async () => {
    apiGet.mockImplementation(
      disclosureGet(
        {
          ...PROVENANCE_BASE,
          disclosure_status: "delivered",
          delivered_at: "2026-08-04T00:00:00Z",
        },
        null
      )
    );

    render(<UnderwritingDetailPage />);
    await screen.findByText(/delivered before document recording/);
    expect(
      screen.queryByRole("button", { name: "Record missing document" })
    ).toBeNull();
  });

  it("will not board a delivered disclosure that has no document", async () => {
    apiGet.mockImplementation(
      disclosureGet(
        {
          ...PROVENANCE_BASE,
          disclosure_status: "delivered",
          delivered_at: "2026-08-04T00:00:00Z",
        },
        null
      )
    );

    render(<UnderwritingDetailPage />);
    const accept = await screen.findByRole("button", { name: "Accept & board" });
    expect(accept.hasAttribute("disabled")).toBe(true);
  });

  it("boards a delivered disclosure that has one", async () => {
    apiGet.mockImplementation(
      disclosureGet(
        {
          ...PROVENANCE_BASE,
          disclosure_status: "delivered",
          delivered_at: "2026-08-04T00:00:00Z",
        },
        GOOD_DOCUMENT
      )
    );

    render(<UnderwritingDetailPage />);
    const accept = await screen.findByRole("button", { name: "Accept & board" });
    expect(accept.hasAttribute("disabled")).toBe(false);
  });
});
